from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.orders_history import get_order_history_impl
from app.mcp_server.tooling.shared import resolve_market_type
from app.services.brokers.upbit.client import fetch_multiple_current_prices_cached
from app.services.exchange_rate_service import get_usd_krw_rate
from app.services.intraday_order_review import (
    check_needs_attention,
    classify_fill_proximity,
    format_fill_proximity,
)
from app.services.market_data import get_quote
from app.services.n8n_formatting import enrich_order_fmt, enrich_summary_fmt

_MARKETS: tuple[str, ...] = ("crypto", "kr", "us")
_EQUITY_QUOTE_CONCURRENCY = 5


def _to_external_market(value: str | None) -> str:
    mapping = {
        "equity_kr": "kr",
        "equity_us": "us",
        "crypto": "crypto",
        "kr": "kr",
        "us": "us",
    }
    return mapping.get(str(value or "").strip().lower(), str(value or "").strip())


def _strip_crypto_prefix(symbol: str) -> str:
    upper = str(symbol or "").strip().upper()
    for prefix in ("KRW-", "USDT-"):
        if upper.startswith(prefix):
            return upper[len(prefix) :]
    return upper


def _parse_created_at(value: str, market: str, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback.replace(microsecond=0)

    if market == "crypto":
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).replace(microsecond=0)

    for fmt in ("%Y%m%d %H%M%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST, microsecond=0)
        except ValueError:
            continue

    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).replace(microsecond=0)


def _infer_market_from_order(order: dict[str, Any]) -> str:
    currency = str(order.get("currency") or "").strip().upper()
    if currency == "USD":
        return "us"

    symbol = str(order.get("symbol") or "").strip().upper()
    if symbol.startswith(("KRW-", "USDT-")):
        return "crypto"
    if len(symbol) == 6 and symbol.isdigit():
        return "kr"

    try:
        market_type, _ = resolve_market_type(symbol, None)
    except ValueError:
        return "kr"
    return _to_external_market(market_type)


async def _fetch_market_batch(
    market: str,
    side: Literal["buy", "sell"] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    result = await get_order_history_impl(
        status="pending",
        market=market,
        side=side,
        limit=-1,
    )
    orders = [dict(order, _market=market) for order in result.get("orders", [])]
    errors = [
        {
            "market": _to_external_market(error.get("market")),
            "error": str(error.get("error") or "unknown error"),
        }
        for error in result.get("errors", [])
    ]
    return orders, errors


async def _fetch_crypto_prices(raw_symbols: list[str]) -> dict[str, float]:
    unique_symbols = sorted({symbol for symbol in raw_symbols if symbol})
    if not unique_symbols:
        return {}
    return await fetch_multiple_current_prices_cached(unique_symbols)


async def _fetch_equity_quotes(
    symbols: list[str],
    market: str,
) -> tuple[dict[str, float], list[dict[str, str]]]:
    unique_symbols = sorted({symbol for symbol in symbols if symbol})
    if not unique_symbols:
        return {}, []

    semaphore = asyncio.Semaphore(_EQUITY_QUOTE_CONCURRENCY)
    prices: dict[str, float] = {}
    errors: list[dict[str, str]] = []

    async def fetch_one(symbol: str) -> None:
        async with semaphore:
            try:
                quote = await get_quote(symbol, market)
            except Exception as exc:  # noqa: BLE001
                errors.append({"market": market, "error": f"{symbol}: {exc}"})
                return
            prices[symbol] = float(quote.price)

    await asyncio.gather(*(fetch_one(symbol) for symbol in unique_symbols))
    return prices, errors


def _normalize_order(
    order: dict[str, Any],
    *,
    as_of: datetime,
    usd_krw_rate: float | None,
) -> dict[str, Any]:
    market = str(
        order.get("_market") or ""
    ).strip().lower() or _infer_market_from_order(order)
    raw_symbol = str(order.get("symbol") or "").strip()
    symbol = _strip_crypto_prefix(raw_symbol) if market == "crypto" else raw_symbol

    created_dt = _parse_created_at(str(order.get("ordered_at") or ""), market, as_of)
    order_price = float(order.get("ordered_price") or 0.0)
    quantity = float(order.get("ordered_qty") or 0.0)
    remaining_qty = float(order.get("remaining_qty") or 0.0)
    base_amount = order_price * remaining_qty
    amount_krw: float | None = base_amount
    if market == "us":
        amount_krw = None if usd_krw_rate is None else base_amount * usd_krw_rate

    age_hours = max(0, int((as_of - created_dt).total_seconds() // 3600))

    return {
        "order_id": str(order.get("order_id") or ""),
        "symbol": symbol,
        "raw_symbol": raw_symbol,
        "market": market,
        "side": str(order.get("side") or ""),
        "status": str(order.get("status") or ""),
        "order_price": order_price,
        "current_price": None,
        "gap_pct": None,
        "amount_krw": amount_krw,
        "quantity": quantity,
        "remaining_qty": remaining_qty,
        "created_at": created_dt.isoformat(),
        "age_hours": age_hours,
        "age_days": age_hours // 24,
        "currency": str(order.get("currency") or ""),
        "_created_dt": created_dt,
    }


def _apply_current_price(order: dict[str, Any], current_price: float | None) -> None:
    order["current_price"] = current_price
    order_price = float(order.get("order_price") or 0.0)
    if current_price is None or order_price <= 0:
        order["gap_pct"] = None
        return
    order["gap_pct"] = round((current_price - order_price) / order_price * 100, 2)


def _build_summary(orders: list[dict[str, Any]]) -> dict[str, float | int]:
    buy_orders = [order for order in orders if order.get("side") == "buy"]
    sell_orders = [order for order in orders if order.get("side") == "sell"]
    return {
        "total": len(orders),
        "buy_count": len(buy_orders),
        "sell_count": len(sell_orders),
        "total_buy_krw": sum(
            float(order["amount_krw"])
            for order in buy_orders
            if order.get("amount_krw") is not None
        ),
        "total_sell_krw": sum(
            float(order["amount_krw"])
            for order in sell_orders
            if order.get("amount_krw") is not None
        ),
    }


async def _enrich_orders_with_market_context(
    orders: list[dict[str, Any]],
    market: str,
    near_fill_pct: float = 2.0,
) -> dict[str, Any]:
    """Enrich orders with fill proximity and attention status using market context.

    Returns:
        Dict with enriched orders and attention counts
    """
    # Lazy import to avoid circular dependency
    from app.services.n8n_market_context_service import fetch_market_context

    # Fetch market context for symbols in these orders
    symbols = [order["symbol"] for order in orders if order.get("symbol")]

    indicators_map: dict[str, dict[str, Any]] = {}
    if symbols:
        try:
            market_ctx = await fetch_market_context(
                market=market,
                symbols=symbols,
                include_fear_greed=False,
                include_economic_calendar=False,
            )
            for ctx in market_ctx.get("symbols", []):
                indicators_map[ctx.symbol] = {
                    "rsi_14": ctx.rsi_14,
                    "change_24h_pct": ctx.change_24h_pct,
                }
        except Exception:
            # Non-fatal: continue without market context
            pass

    enriched_orders = []
    near_fill_count = 0
    needs_attention_count = 0
    attention_orders = []

    for order in orders:
        gap_pct = order.get("gap_pct")
        symbol = order.get("symbol", "")

        # Classify fill proximity
        proximity = classify_fill_proximity(gap_pct, {"near": near_fill_pct})
        order["fill_proximity"] = proximity
        order["fill_proximity_fmt"] = format_fill_proximity(proximity, gap_pct)

        if proximity == "near":
            near_fill_count += 1

        # Check attention needs
        indicators = indicators_map.get(symbol, {})
        needs_attention, attention_reason = check_needs_attention(
            order,
            indicators,
            {"near_fill_pct": near_fill_pct},
        )

        order["needs_attention"] = needs_attention
        order["attention_reason"] = attention_reason

        if needs_attention:
            needs_attention_count += 1
            attention_orders.append(order)

        enriched_orders.append(order)

    return {
        "orders": enriched_orders,
        "near_fill_count": near_fill_count,
        "needs_attention_count": needs_attention_count,
        "attention_orders": attention_orders,
    }


async def fetch_pending_orders(
    *,
    market: Literal["crypto", "kr", "us", "all"] = "all",
    min_amount: float = 0,
    include_current_price: bool = True,
    side: Literal["buy", "sell"] | None = None,
    as_of: datetime | None = None,
    attention_only: bool = False,
    near_fill_pct: float = 2.0,
) -> dict[str, Any]:
    requested_markets = list(_MARKETS if market == "all" else (market,))
    effective_as_of = as_of or now_kst().replace(microsecond=0)

    batch_results = await asyncio.gather(
        *(
            _fetch_market_batch(requested_market, side)
            for requested_market in requested_markets
        )
    )

    source_orders: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for batch_orders, batch_errors in batch_results:
        source_orders.extend(batch_orders)
        errors.extend(batch_errors)

    usd_krw_rate: float | None = None
    if any(str(order.get("_market") or "") == "us" for order in source_orders):
        try:
            usd_krw_rate = await get_usd_krw_rate()
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {"market": "us", "error": f"USD/KRW rate fetch failed: {exc}"}
            )

    normalized_orders = [
        _normalize_order(order, as_of=effective_as_of, usd_krw_rate=usd_krw_rate)
        for order in source_orders
    ]

    if include_current_price:
        (
            crypto_prices,
            (kr_prices, kr_errors),
            (us_prices, us_errors),
        ) = await asyncio.gather(
            _fetch_crypto_prices(
                [
                    order["raw_symbol"]
                    for order in normalized_orders
                    if order["market"] == "crypto"
                ]
            ),
            _fetch_equity_quotes(
                [
                    order["raw_symbol"]
                    for order in normalized_orders
                    if order["market"] == "kr"
                ],
                "kr",
            ),
            _fetch_equity_quotes(
                [
                    order["raw_symbol"]
                    for order in normalized_orders
                    if order["market"] == "us"
                ],
                "us",
            ),
        )
        errors.extend(kr_errors)
        errors.extend(us_errors)

        for order in normalized_orders:
            current_price: float | None = None
            if order["market"] == "crypto":
                current_price = crypto_prices.get(order["raw_symbol"])
            elif order["market"] == "kr":
                current_price = kr_prices.get(order["raw_symbol"])
            elif order["market"] == "us":
                current_price = us_prices.get(order["raw_symbol"])
            _apply_current_price(order, current_price)

    filtered_orders = [
        order
        for order in normalized_orders
        if order.get("amount_krw") is None
        or float(order["amount_krw"]) >= float(min_amount)
    ]
    filtered_orders.sort(key=lambda order: order["_created_dt"])

    for order in filtered_orders:
        order.pop("_created_dt", None)

    for order in filtered_orders:
        enrich_order_fmt(order)

    # Enrich with fill proximity and attention status if current price is included
    if include_current_price:
        enrichment = await _enrich_orders_with_market_context(
            filtered_orders,
            market,
            near_fill_pct=near_fill_pct,
        )
        filtered_orders = enrichment["orders"]
    else:
        enrichment = {
            "near_fill_count": 0,
            "needs_attention_count": 0,
            "attention_orders": [],
        }

    # Filter to attention-only if requested
    if attention_only:
        filtered_orders = enrichment["attention_orders"]

    summary = _build_summary(filtered_orders)
    summary["near_fill_count"] = enrichment["near_fill_count"]
    summary["needs_attention_count"] = enrichment["needs_attention_count"]
    summary["attention_orders_only"] = (
        enrichment["attention_orders"] if attention_only else []
    )
    enrich_summary_fmt(summary, as_of=effective_as_of)

    return {
        "success": bool(filtered_orders) or not errors,
        "market": market,
        "orders": filtered_orders,
        "summary": summary,
        "errors": errors,
    }


__all__ = ["fetch_pending_orders"]
