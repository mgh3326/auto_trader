"""Filled-orders service — fetches recent fills across all markets."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.timezone import KST, now_kst
from app.services.brokers.kis.client import KISClient
from app.services.market_data import get_quote

logger = logging.getLogger(__name__)

_EQUITY_QUOTE_CONCURRENCY = 5
_US_EXCHANGES = ("NASD", "NYSE", "AMEX")


def _strip_crypto_prefix(symbol: str) -> str:
    upper = str(symbol or "").strip().upper()
    for prefix in ("KRW-", "USDT-"):
        if upper.startswith(prefix):
            return upper[len(prefix) :]
    return upper


def _normalize_upbit_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    if order.get("state") != "done":
        return None

    executed_vol = float(order.get("executed_volume") or 0)
    if executed_vol <= 0:
        return None

    price = float(order.get("price") or 0)
    total = price * executed_vol
    raw_symbol = str(order.get("market", ""))
    side_raw = str(order.get("side", "")).lower()

    return {
        "symbol": _strip_crypto_prefix(raw_symbol),
        "raw_symbol": raw_symbol,
        "instrument_type": "crypto",
        "side": "buy" if side_raw == "bid" else "sell",
        "price": price,
        "quantity": executed_vol,
        "total_amount": total,
        "fee": float(order.get("paid_fee") or 0),
        "currency": "KRW",
        "account": "upbit",
        "order_id": str(order.get("uuid", "")),
        "filled_at": str(order.get("created_at", "")),
    }


def _parse_filled_at_kst(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    else:
        parsed = parsed.astimezone(KST)

    return parsed


def _normalize_kis_domestic_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    qty = float(order.get("ccld_qty") or order.get("tot_ccld_qty") or 0)
    if qty <= 0:
        return None

    price = float(order.get("ccld_unpr") or order.get("avg_prvs") or 0)
    total = float(order.get("ccld_amt") or order.get("tot_ccld_amt") or price * qty)
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd") or order.get("ccld_tmd") or "000000")

    filled_at_str = ord_dt
    if len(ord_dt) == 8 and len(ord_tmd) >= 6:
        from datetime import datetime

        try:
            dt = datetime.strptime(f"{ord_dt} {ord_tmd[:6]}", "%Y%m%d %H%M%S")
            filled_at_str = dt.replace(tzinfo=KST).isoformat()
        except ValueError:
            pass

    symbol = str(order.get("pdno") or order.get("stck_code") or "").strip()
    side_code = str(order.get("sll_buy_dvsn_cd") or "")

    return {
        "symbol": symbol,
        "raw_symbol": symbol,
        "instrument_type": "equity_kr",
        "side": "sell" if side_code == "01" else "buy",
        "price": price,
        "quantity": qty,
        "total_amount": total,
        "fee": 0,
        "currency": "KRW",
        "account": "kis",
        "order_id": str(order.get("ord_no") or order.get("odno") or ""),
        "filled_at": filled_at_str,
    }


def _normalize_kis_overseas_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    qty = float(order.get("ft_ccld_qty") or order.get("ccld_qty") or 0)
    if qty <= 0:
        return None

    price = float(order.get("ft_ccld_unpr3") or order.get("ccld_unpr") or 0)
    total = float(order.get("ft_ccld_amt3") or order.get("ccld_amt") or price * qty)
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd") or "000000")

    filled_at_str = ord_dt
    if len(ord_dt) == 8 and len(ord_tmd) >= 6:
        from datetime import datetime

        try:
            dt = datetime.strptime(f"{ord_dt} {ord_tmd[:6]}", "%Y%m%d %H%M%S")
            filled_at_str = dt.replace(tzinfo=KST).isoformat()
        except ValueError:
            pass

    symbol = str(order.get("pdno") or order.get("symb") or "").strip()

    return {
        "symbol": symbol,
        "raw_symbol": symbol,
        "instrument_type": "equity_us",
        "side": "sell" if str(order.get("sll_buy_dvsn_cd", "")) == "01" else "buy",
        "price": price,
        "quantity": qty,
        "total_amount": total,
        "fee": 0,
        "currency": "USD",
        "account": "kis_overseas",
        "order_id": str(order.get("odno") or order.get("ord_no") or ""),
        "filled_at": filled_at_str,
    }


async def _fetch_upbit_filled(days: int) -> tuple[list[dict], list[dict]]:
    try:
        closed = await upbit_service.fetch_closed_orders(market=None, limit=100)
        orders = [
            n for raw in closed if (n := _normalize_upbit_filled(raw)) is not None
        ]
        cutoff = now_kst() - timedelta(days=days)
        filtered_orders: list[dict[str, Any]] = []

        for order in orders:
            parsed_filled_at = _parse_filled_at_kst(order.get("filled_at", ""))
            if parsed_filled_at is None:
                logger.warning(
                    "Upbit filled order skipped due to invalid filled_at: order_id=%s filled_at=%r",
                    order.get("order_id"),
                    order.get("filled_at"),
                )
                continue
            if parsed_filled_at >= cutoff:
                filtered_orders.append(order)

        return filtered_orders, []
    except Exception as exc:
        logger.warning("Upbit filled-orders fetch failed: %s", exc)
        return [], [{"market": "crypto", "error": str(exc)}]


async def _fetch_kis_domestic_filled(days: int) -> tuple[list[dict], list[dict]]:
    try:
        kis = KISClient()
        end_date = now_kst().strftime("%Y%m%d")
        start_date = (now_kst() - timedelta(days=days)).strftime("%Y%m%d")
        raw_orders = await kis.inquire_daily_order_domestic(
            start_date=start_date, end_date=end_date, stock_code="", side="00"
        )
        orders = [
            n
            for raw in (raw_orders or [])
            if (n := _normalize_kis_domestic_filled(raw)) is not None
        ]
        return orders, []
    except Exception as exc:
        logger.warning("KIS domestic filled-orders fetch failed: %s", exc)
        return [], [{"market": "kr", "error": str(exc)}]


async def _fetch_kis_overseas_filled(days: int) -> tuple[list[dict], list[dict]]:
    try:
        kis = KISClient()
        end_date = now_kst().strftime("%Y%m%d")
        start_date = (now_kst() - timedelta(days=days)).strftime("%Y%m%d")

        all_orders: list[dict] = []
        seen_order_ids: set[str] = set()

        for exchange in _US_EXCHANGES:
            try:
                raw_orders = await kis.inquire_daily_order_overseas(
                    start_date=start_date,
                    end_date=end_date,
                    symbol="%",
                    exchange_code=exchange,
                    side="00",
                )
                for raw in raw_orders or []:
                    normalized = _normalize_kis_overseas_filled(raw)
                    if normalized and normalized["order_id"] not in seen_order_ids:
                        seen_order_ids.add(normalized["order_id"])
                        all_orders.append(normalized)
            except Exception as exc:
                logger.warning("KIS overseas %s fetch failed: %s", exchange, exc)

        return all_orders, []
    except Exception as exc:
        logger.warning("KIS overseas filled-orders fetch failed: %s", exc)
        return [], [{"market": "us", "error": str(exc)}]


async def _enrich_with_current_prices(
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: dict[str, float | None] = {}
    sem = asyncio.Semaphore(_EQUITY_QUOTE_CONCURRENCY)

    crypto_symbols = list(
        {o["raw_symbol"] for o in orders if o["instrument_type"] == "crypto"}
    )
    if crypto_symbols:
        try:
            prices = await upbit_service.fetch_multiple_current_prices_cached(
                crypto_symbols
            )
            for sym, price in prices.items():
                seen[sym] = price
        except Exception as exc:
            logger.warning("Crypto batch price fetch failed: %s", exc)

    equity_symbols = list(
        {
            (o["raw_symbol"], o["instrument_type"])
            for o in orders
            if o["instrument_type"] in ("equity_kr", "equity_us")
        }
    )

    async def _fetch_one(symbol: str, itype: str) -> None:
        if symbol in seen:
            return
        async with sem:
            try:
                market = "kr" if itype == "equity_kr" else "us"
                quote = await get_quote(symbol, market=market)
                seen[symbol] = quote.price or None
            except Exception as exc:
                logger.warning("Quote fetch failed for %s: %s", symbol, exc)
                seen[symbol] = None

    await asyncio.gather(
        *[_fetch_one(sym, itype) for sym, itype in equity_symbols],
        return_exceptions=True,
    )

    for order in orders:
        cp = seen.get(order["raw_symbol"])
        order["current_price"] = cp
        if cp and order["price"]:
            if order["side"] == "buy":
                pnl = ((cp - order["price"]) / order["price"]) * 100
            else:
                pnl = ((order["price"] - cp) / order["price"]) * 100
            order["pnl_pct"] = round(pnl, 2)
            sign = "+" if pnl >= 0 else ""
            order["pnl_pct_fmt"] = f"{sign}{pnl:.2f}%"
        else:
            order["pnl_pct"] = None
            order["pnl_pct_fmt"] = None

    return orders


async def fetch_filled_orders(
    days: int = 1,
    markets: str = "crypto,kr,us",
    min_amount: float = 0,
) -> dict[str, Any]:
    market_set = {m.strip().lower() for m in markets.split(",") if m.strip()}
    all_orders: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []

    tasks = []
    if "crypto" in market_set:
        tasks.append(_fetch_upbit_filled(days))
    if "kr" in market_set:
        tasks.append(_fetch_kis_domestic_filled(days))
    if "us" in market_set:
        tasks.append(_fetch_kis_overseas_filled(days))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            all_errors.append({"error": str(result)})
        elif isinstance(result, tuple):
            orders, errors = result
            all_orders.extend(orders)
            all_errors.extend(errors)

    if min_amount > 0:
        all_orders = [o for o in all_orders if o.get("total_amount", 0) >= min_amount]

    if all_orders:
        all_orders = await _enrich_with_current_prices(all_orders)

    all_orders.sort(key=lambda o: o.get("filled_at", ""), reverse=True)

    return {"orders": all_orders, "errors": all_errors}
