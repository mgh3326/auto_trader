"""Order history helpers and shared order execution aliases."""

from __future__ import annotations

from typing import Any, Literal

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling.order_execution import (
    _calculate_date_range,
    _normalize_market_type_to_external,
)
from app.mcp_server.tooling.orders_modify_cancel import (
    _extract_kis_order_number,
    _get_kis_field,
    _normalize_kis_domestic_order,
    _normalize_kis_overseas_order,
    _normalize_upbit_order,
)
from app.mcp_server.tooling.shared import (
    logger,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.services.brokers.kis.client import KISClient
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol


def _create_kis_client(*, is_mock: bool) -> KISClient:
    if is_mock:
        return KISClient(is_mock=True)
    return KISClient()


async def _call_kis(method: Any, *args: Any, is_mock: bool, **kwargs: Any) -> Any:
    if is_mock:
        return await method(*args, **kwargs, is_mock=True)
    return await method(*args, **kwargs)


def _calculate_order_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
    total_orders = len(orders)
    filled = sum(1 for o in orders if o.get("status") == "filled")
    pending = sum(1 for o in orders if o.get("status") == "pending")
    partial = sum(1 for o in orders if o.get("status") == "partial")
    cancelled = sum(1 for o in orders if o.get("status") == "cancelled")

    return {
        "total_orders": total_orders,
        "filled": filled,
        "pending": pending,
        "partial": partial,
        "cancelled": cancelled,
    }


def _validate_history_inputs(
    symbol: str | None,
    status: str,
    order_id: str | None,
    market: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
    float,
    list[str],
    str | None,
]:
    """Validate and normalize history query inputs.

    Returns:
        (symbol, order_id, market_hint, side, effective_days,
         limit_val, market_types, normalized_symbol)
    """
    if status != "pending" and not symbol:
        raise ValueError(
            f"symbol is required when status='{status}'. "
            "Use status='pending' for symbol-free queries, "
            "or provide a symbol (e.g. symbol='KRW-BTC')."
        )

    symbol = (symbol or "").strip() or None
    order_id = (order_id or "").strip() or None
    market_hint = (market or "").strip().lower() or None
    side = (side or "").strip().lower() or None

    if side and side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")

    if limit is None:
        limit = 50
    elif limit < -1:
        raise ValueError("limit must be >= -1")

    limit_val = limit if limit not in (0, -1) else float("inf")
    effective_days = days

    market_types: list[str] = []
    normalized_symbol: str | None = None

    if symbol:
        market_type, normalized_symbol = _resolve_market_type(symbol, market_hint)
        market_types = [market_type]
    elif market_hint:
        norm = _normalize_market(market_hint)
        if norm:
            market_types = [norm]

    if not market_types and status == "pending":
        market_types = ["crypto", "equity_kr", "equity_us"]

    if not market_types and order_id:
        if "-" in order_id and len(order_id) == 36:
            market_types = ["crypto"]
        else:
            market_types = ["crypto", "equity_kr", "equity_us"]

    return (
        symbol,
        order_id,
        market_hint,
        side,
        effective_days,
        limit_val,
        market_types,
        normalized_symbol,
    )


async def _fetch_crypto_orders(
    normalized_symbol: str | None,
    status: str,
    limit_val: float,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch and normalize Upbit (crypto) orders."""
    fetched: list[dict[str, Any]] = []

    if status in ("all", "pending"):
        open_ops = await upbit_service.fetch_open_orders(market=normalized_symbol)
        fetched.extend([_normalize_upbit_order(o) for o in open_ops])

    if status in ("all", "filled", "cancelled") and normalized_symbol:
        fetch_limit = 100 if limit_val == float("inf") else max(limit, 20)
        closed_ops = await upbit_service.fetch_closed_orders(
            market=normalized_symbol,
            limit=fetch_limit,
        )
        fetched.extend([_normalize_upbit_order(o) for o in closed_ops])

    return fetched


async def _fetch_kr_orders(
    normalized_symbol: str | None,
    status: str,
    effective_days: int | None,
    is_mock: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and normalize KIS domestic (Korean equity) orders."""
    fetched: list[dict[str, Any]] = []
    kis = _create_kis_client(is_mock=is_mock)

    if status in ("all", "pending"):
        logger.debug("Fetching KR pending orders, symbol=%s", normalized_symbol)
        open_ops = await _call_kis(kis.inquire_korea_orders, is_mock=is_mock)
        if open_ops:
            logger.debug("Raw API response keys: %s", list(open_ops[0].keys()))
        for o in open_ops:
            o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
            if normalized_symbol and o_sym != normalized_symbol:
                continue
            fetched.append(_normalize_kis_domestic_order(o))

    if status in ("all", "filled", "cancelled") and normalized_symbol:
        lookup_days = effective_days if effective_days is not None else 30
        start_dt, end_dt = _calculate_date_range(lookup_days)
        hist_ops = await _call_kis(
            kis.inquire_daily_order_domestic,
            start_date=start_dt,
            end_date=end_dt,
            stock_code=normalized_symbol,
            side="00",
            is_mock=is_mock,
        )
        fetched.extend([_normalize_kis_domestic_order(o) for o in hist_ops])

    return fetched


async def _fetch_us_orders(
    normalized_symbol: str | None,
    status: str,
    effective_days: int | None,
    is_mock: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and normalize KIS overseas (US equity) orders."""
    fetched: list[dict[str, Any]] = []
    kis = _create_kis_client(is_mock=is_mock)

    if status in ("all", "pending"):
        target_exchanges = ["NASD", "NYSE", "AMEX"]
        if normalized_symbol:
            target_exchanges = [await get_us_exchange_by_symbol(normalized_symbol)]

        seen_oids: set[str] = set()
        for ex in target_exchanges:
            try:
                ops = await _call_kis(
                    kis.inquire_overseas_orders,
                    ex,
                    is_mock=is_mock,
                )
                for o in ops:
                    oid = _extract_kis_order_number(o)
                    if oid in seen_oids:
                        continue
                    seen_oids.add(oid)

                    o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
                    if normalized_symbol and o_sym != normalized_symbol:
                        continue
                    fetched.append(_normalize_kis_overseas_order(o))
            except Exception:
                pass

    if status in ("all", "filled", "cancelled") and normalized_symbol:
        lookup_days = effective_days if effective_days is not None else 30
        start_dt, end_dt = _calculate_date_range(lookup_days)
        ex = await get_us_exchange_by_symbol(normalized_symbol)
        hist_ops = await _call_kis(
            kis.inquire_daily_order_overseas,
            start_date=start_dt,
            end_date=end_dt,
            symbol=normalized_symbol,
            exchange_code=ex,
            side="00",
            is_mock=is_mock,
        )
        fetched.extend([_normalize_kis_overseas_order(o) for o in hist_ops])

    return fetched


def _dedupe_orders(
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate orders, preserving last occurrence."""
    original_count = len(orders)
    unique_orders: dict[tuple[Any, ...], dict[str, Any]] = {}
    for o in orders:
        oid = str(o.get("order_id") or "").strip()
        source_market = o.get("_source_market") or o.get("market") or "unknown"
        if oid:
            key = (source_market, oid)
            unique_orders[key] = o
        else:
            key = (
                source_market,
                o.get("symbol"),
                o.get("side"),
                o.get("ordered_price"),
                o.get("ordered_qty"),
                o.get("ordered_at"),
                o.get("status"),
                o.get("currency"),
            )
            unique_orders[key] = o

    result = list(unique_orders.values())
    removed = original_count - len(result)
    if removed > 0:
        logger.info("Removed %s duplicate orders", removed)
    return result


def _filter_and_sort_orders(
    orders: list[dict[str, Any]],
    status: str,
    order_id: str | None,
    side: str | None,
    limit_val: float,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Filter, sort, and truncate orders.

    Returns:
        (response_orders, total_available, truncated)
    """
    filtered: list[dict[str, Any]] = []
    for o in orders:
        o_status = o.get("status")
        if status == "pending":
            if o_status not in ("pending", "partial"):
                continue
        elif status == "filled":
            if o_status != "filled":
                continue
        elif status == "cancelled":
            if o_status != "cancelled":
                continue

        if order_id and o.get("order_id") != order_id:
            continue

        if side and o.get("side") != side:
            continue

        filtered.append(o)

    def _get_sort_key(o: dict[str, Any]) -> str:
        val = o.get("ordered_at") or o.get("created_at") or ""
        return str(val)

    filtered.sort(key=_get_sort_key, reverse=True)

    total_available = len(filtered)
    truncated = False
    if limit_val != float("inf") and total_available > limit_val:
        filtered = filtered[: int(limit_val)]
        truncated = True

    response_orders: list[dict[str, Any]] = []
    for o in filtered:
        cleaned = dict(o)
        cleaned.pop("_source_market", None)
        response_orders.append(cleaned)

    return response_orders, total_available, truncated


def _build_history_response(
    *,
    response_orders: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    market_types: list[str],
    normalized_symbol: str | None,
    symbol: str | None,
    status: str,
    order_id: str | None,
    market_hint: str | None,
    side: str | None,
    days: int | None,
    limit: int | None,
    truncated: bool,
    total_available: int,
) -> dict[str, Any]:
    """Build the final order history response dict."""
    summary = _calculate_order_summary(response_orders)

    ret_market = "mixed"
    if len(market_types) == 1:
        ret_market = _normalize_market_type_to_external(market_types[0])
    elif normalized_symbol:
        m, _ = _resolve_market_type(normalized_symbol, None)
        ret_market = _normalize_market_type_to_external(m)

    return {
        "success": bool(response_orders) or not errors,
        "symbol": normalized_symbol,
        "market": ret_market,
        "status": status,
        "filters": {
            "symbol": symbol,
            "status": status,
            "order_id": order_id,
            "market": market_hint,
            "side": side,
            "days": days,
            "limit": limit,
        },
        "orders": response_orders,
        "summary": summary,
        "truncated": truncated,
        "total_available": total_available,
        "errors": errors,
    }


async def get_order_history_impl(
    symbol: str | None = None,
    status: Literal["all", "pending", "filled", "cancelled"] = "all",
    order_id: str | None = None,
    market: str | None = None,
    side: str | None = None,
    days: int | None = None,
    limit: int | None = 50,
    is_mock: bool = False,
) -> dict[str, Any]:
    (
        symbol,
        order_id,
        market_hint,
        side,
        effective_days,
        limit_val,
        market_types,
        normalized_symbol,
    ) = _validate_history_inputs(symbol, status, order_id, market, side, days, limit)

    _broker_fetchers = {
        "crypto": lambda: _fetch_crypto_orders(
            normalized_symbol, status, limit_val, limit or 50
        ),
        "equity_kr": lambda: _fetch_kr_orders(
            normalized_symbol, status, effective_days, is_mock=is_mock
        ),
        "equity_us": lambda: _fetch_us_orders(
            normalized_symbol, status, effective_days, is_mock=is_mock
        ),
    }

    orders: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for m_type in market_types:
        try:
            fetcher = _broker_fetchers.get(m_type)
            if fetcher is None:
                continue
            fetched = await fetcher()
            source_market = _normalize_market_type_to_external(m_type)
            for f in fetched:
                f["_source_market"] = source_market
            orders.extend(fetched)
        except Exception as e:
            errors.append({"market": m_type, "error": str(e)})

    orders = _dedupe_orders(orders)
    response_orders, total_available, truncated = _filter_and_sort_orders(
        orders,
        status,
        order_id,
        side,
        limit_val,
    )

    return _build_history_response(
        response_orders=response_orders,
        errors=errors,
        market_types=market_types,
        normalized_symbol=normalized_symbol,
        symbol=symbol,
        status=status,
        order_id=order_id,
        market_hint=market_hint,
        side=side,
        days=days,
        limit=limit,
        truncated=truncated,
        total_available=total_available,
    )


__all__ = [
    "get_order_history_impl",
]
