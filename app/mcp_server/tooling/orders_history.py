"""Order history helpers and shared order execution aliases."""

from __future__ import annotations

from typing import Any, Literal

from app.mcp_server.tooling import order_execution as _order_execution
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
from app.services import upbit as upbit_service
from app.services.kis import KISClient
from data.stocks_info.overseas_us_stocks import get_exchange_by_symbol

_calculate_date_range = _order_execution._calculate_date_range
_normalize_market_type_to_external = _order_execution._normalize_market_type_to_external
_get_current_price_for_order = _order_execution._get_current_price_for_order
_get_holdings_for_order = _order_execution._get_holdings_for_order
_get_balance_for_order = _order_execution._get_balance_for_order
_check_daily_order_limit = _order_execution._check_daily_order_limit
_record_order_history = _order_execution._record_order_history
_preview_order = _order_execution._preview_order
_execute_order = _order_execution._execute_order
_place_order_impl = _order_execution._place_order_impl


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


async def get_order_history_impl(
    symbol: str | None = None,
    status: Literal["all", "pending", "filled", "cancelled"] = "all",
    order_id: str | None = None,
    market: str | None = None,
    side: str | None = None,
    days: int | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    if status != "pending" and not symbol:
        raise ValueError(
            f"symbol is required when status='{status}'. "
            "Use status='pending' for symbol-free queries, or provide a symbol (e.g. symbol='KRW-BTC')."
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

    orders: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for m_type in market_types:
        try:
            fetched: list[dict[str, Any]] = []

            if m_type == "crypto":
                if status in ("all", "pending"):
                    open_ops = await upbit_service.fetch_open_orders(
                        market=normalized_symbol
                    )
                    fetched.extend([_normalize_upbit_order(o) for o in open_ops])

                if status in ("all", "filled", "cancelled") and normalized_symbol:
                    fetch_limit = 100 if limit_val == float("inf") else max(limit, 20)
                    closed_ops = await upbit_service.fetch_closed_orders(
                        market=normalized_symbol,
                        limit=fetch_limit,
                    )
                    fetched.extend([_normalize_upbit_order(o) for o in closed_ops])

            elif m_type == "equity_kr":
                kis = KISClient()
                if status in ("all", "pending"):
                    logger.debug(
                        "Fetching KR pending orders, symbol=%s", normalized_symbol
                    )
                    open_ops = await kis.inquire_korea_orders()
                    if open_ops:
                        logger.debug(
                            "Raw API response keys: %s", list(open_ops[0].keys())
                        )
                    for o in open_ops:
                        o_sym = str(_get_kis_field(o, "pdno", "PDNO"))
                        if normalized_symbol and o_sym != normalized_symbol:
                            continue
                        fetched.append(_normalize_kis_domestic_order(o))

                if status in ("all", "filled", "cancelled") and normalized_symbol:
                    lookup_days = effective_days if effective_days is not None else 30
                    start_dt, end_dt = _calculate_date_range(lookup_days)
                    hist_ops = await kis.inquire_daily_order_domestic(
                        start_date=start_dt,
                        end_date=end_dt,
                        stock_code=normalized_symbol,
                        side="00",
                    )
                    fetched.extend([_normalize_kis_domestic_order(o) for o in hist_ops])

            elif m_type == "equity_us":
                kis = KISClient()
                if status in ("all", "pending"):
                    target_exchanges = ["NASD", "NYSE", "AMEX"]
                    if normalized_symbol:
                        ex = get_exchange_by_symbol(normalized_symbol)
                        if ex:
                            target_exchanges = [ex]

                    seen_oids = set()
                    for ex in target_exchanges:
                        try:
                            ops = await kis.inquire_overseas_orders(ex)
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
                    ex = get_exchange_by_symbol(normalized_symbol) or "NASD"
                    hist_ops = await kis.inquire_daily_order_overseas(
                        start_date=start_dt,
                        end_date=end_dt,
                        symbol=normalized_symbol,
                        exchange_code=ex,
                        side="00",
                    )
                    fetched.extend([_normalize_kis_overseas_order(o) for o in hist_ops])

            source_market = _normalize_market_type_to_external(m_type)
            for f in fetched:
                f["_source_market"] = source_market
            orders.extend(fetched)

        except Exception as e:
            errors.append({"market": m_type, "error": str(e)})

    original_order_count = len(orders)
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

    orders = list(unique_orders.values())
    removed_duplicates = original_order_count - len(orders)
    if removed_duplicates > 0:
        logger.info("Removed %s duplicate orders", removed_duplicates)

    filtered_orders: list[dict[str, Any]] = []
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

        filtered_orders.append(o)

    def _get_sort_key(o: dict[str, Any]) -> str:
        val = o.get("ordered_at") or o.get("created_at") or ""
        return str(val)

    filtered_orders.sort(key=_get_sort_key, reverse=True)

    total_available = len(filtered_orders)
    truncated = False
    if limit_val != float("inf") and total_available > limit_val:
        filtered_orders = filtered_orders[: int(limit_val)]
        truncated = True

    response_orders: list[dict[str, Any]] = []
    for o in filtered_orders:
        cleaned = dict(o)
        cleaned.pop("_source_market", None)
        response_orders.append(cleaned)

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


__all__ = [
    "get_order_history_impl",
    "_place_order_impl",
    "_calculate_date_range",
    "_normalize_market_type_to_external",
    "_get_current_price_for_order",
    "_get_holdings_for_order",
    "_get_balance_for_order",
    "_check_daily_order_limit",
    "_record_order_history",
    "_preview_order",
    "_execute_order",
]
