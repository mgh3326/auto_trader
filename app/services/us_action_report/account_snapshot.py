from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.symbol import to_db_symbol
from app.schemas.us_action_report import KISUSAccountSnapshot, USHolding, USOpenOrder

_NUMERIC_BLANKS = {"", "-", "--", "N/A", "None", None}
_US_COUNTRY_MARKERS = {
    "US",
    "USA",
    "UNITED STATES",
    "UNITED STATES OF AMERICA",
    "840",
    "미국",
}
_READ_ONLY_ORDER_METHODS = (
    "inquire_overseas_orders",
    "fetch_open_overseas_orders",
    "fetch_open_orders",
)


def _to_float(value: Any) -> float | None:
    if value in _NUMERIC_BLANKS:
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if value in _NUMERIC_BLANKS:
                return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _first_str(row: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _is_us_holding(row: Mapping[str, Any]) -> bool:
    source_markers = {
        str(row.get("source") or "").strip().lower(),
        str(row.get("broker_type") or "").strip().lower(),
        str(row.get("account_kind") or row.get("accountKind") or "").strip().lower(),
    }
    if source_markers & {
        "toss",
        "toss_manual",
        "manual",
        "pension_manual",
        "isa_manual",
    }:
        return False

    country_values = [
        row.get("natn_cd"),
        row.get("nation_code"),
        row.get("natn_kor_name"),
        row.get("natn_name"),
        row.get("country"),
        row.get("tr_mket_name"),
    ]
    present = [
        str(value).strip().upper()
        for value in country_values
        if value not in _NUMERIC_BLANKS
    ]
    if not present:
        return True
    return any(value in _US_COUNTRY_MARKERS for value in present)


def _is_usd_margin_row(row: Mapping[str, Any]) -> bool:
    currency = str(row.get("crcy_cd") or row.get("currency") or "").strip().upper()
    nation = str(row.get("natn_name") or row.get("natn_kor_name") or "").strip().upper()
    return currency == "USD" and (not nation or nation in _US_COUNTRY_MARKERS)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_noarg_or_kwargs(method: Any, **kwargs: Any) -> Any:
    try:
        return await _maybe_await(method(**kwargs))
    except TypeError:
        return await _maybe_await(method())


async def _load_margin(
    kis_client: Any, warnings: list[str]
) -> tuple[float | None, float | None]:
    try:
        method = getattr(kis_client, "inquire_overseas_margin", None)
        if callable(method):
            rows = await _call_noarg_or_kwargs(method, is_mock=False)
        else:
            account = getattr(kis_client, "account", None)
            method = getattr(account, "inquire_overseas_margin", None)
            if not callable(method):
                warnings.append(
                    "kis_live_us_margin_unavailable: no margin reader on client"
                )
                return None, None
            rows = await _call_noarg_or_kwargs(method)
    except (
        Exception
    ) as exc:  # pragma: no cover - exercised by tests without matching exact type
        warnings.append(f"kis_live_us_margin_unavailable: {exc}")
        return None, None

    for row in rows or []:
        if isinstance(row, Mapping) and _is_usd_margin_row(row):
            return (
                _first_float(row, ("frcr_dncl_amt1", "cash_usd", "usd_cash")),
                _first_float(
                    row, ("frcr_ord_psbl_amt1", "buying_power_usd", "usd_buying_power")
                ),
            )
    warnings.append("kis_live_us_margin_unavailable: USD margin row not found")
    return None, None


async def _load_holdings(
    kis_client: Any, warnings: list[str]
) -> list[Mapping[str, Any]]:
    method = getattr(kis_client, "fetch_my_overseas_stocks", None)
    if callable(method):
        try:
            rows = await _call_noarg_or_kwargs(method, is_mock=False)
            return [
                row
                for row in rows or []
                if isinstance(row, Mapping) and _is_us_holding(row)
            ]
        except Exception as exc:
            warnings.append(f"kis_live_us_holdings_unavailable: {exc}")
            return []

    method = getattr(kis_client, "fetch_my_us_stocks", None)
    if callable(method):
        try:
            rows = await _call_noarg_or_kwargs(method, is_mock=False)
            return [row for row in rows or [] if isinstance(row, Mapping)]
        except Exception as exc:
            warnings.append(f"kis_live_us_holdings_unavailable: {exc}")
            return []

    warnings.append("kis_live_us_holdings_unavailable: no holdings reader on client")
    return []


async def _load_open_orders(
    kis_client: Any, warnings: list[str]
) -> list[Mapping[str, Any]]:
    errors: list[str] = []
    for method_name in _READ_ONLY_ORDER_METHODS:
        method = getattr(kis_client, method_name, None)
        if not callable(method):
            continue
        all_rows: list[Mapping[str, Any]] = []
        for exchange_code in ("NASD", "NYSE", "AMEX"):
            try:
                rows = await _call_noarg_or_kwargs(
                    method, exchange_code=exchange_code, is_mock=False
                )
            except Exception as exc:
                errors.append(f"{exchange_code}: {exc}")
                continue
            all_rows.extend(row for row in rows or [] if isinstance(row, Mapping))
        if all_rows:
            return all_rows
        if errors:
            warnings.append("kis_live_us_open_orders_unavailable: " + "; ".join(errors))
        return []
    return []


def _order_symbol(row: Mapping[str, Any]) -> str | None:
    symbol = _first_str(row, ("ovrs_pdno", "pdno", "symbol", "ticker"))
    return to_db_symbol(symbol.upper()) if symbol else None


def _order_pending_qty(row: Mapping[str, Any]) -> float:
    return (
        _first_float(
            row,
            (
                "nccs_qty",
                "ord_unpr_qty",
                "remaining_qty",
                "remainingQty",
                "rmn_qty",
                "qty",
                "ft_ord_qty",
            ),
        )
        or 0.0
    )


def _order_side(row: Mapping[str, Any]) -> str:
    raw = (
        str(
            row.get("sll_buy_dvsn_cd")
            or row.get("sll_buy_dvsn_name")
            or row.get("side")
            or row.get("buy_sell")
            or ""
        )
        .strip()
        .lower()
    )
    if raw in {"02", "buy", "b", "매수"}:
        return "buy"
    if raw in {"01", "sell", "s", "매도"}:
        return "sell"
    return "unknown"


def _build_open_orders(rows: list[Mapping[str, Any]]) -> list[USOpenOrder]:
    orders: list[USOpenOrder] = []
    seen: set[tuple[str | None, str, str, float]] = set()
    for row in rows:
        symbol = _order_symbol(row)
        if not symbol:
            continue
        pending_qty = _order_pending_qty(row)
        side = _order_side(row)
        order_id = _first_str(row, ("odno", "order_id", "orderId"))
        dedupe_key = (order_id, symbol, side, pending_qty)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        orders.append(
            USOpenOrder(
                symbol=symbol,
                side=side,
                quantity=_first_float(
                    row, ("qty", "ord_qty", "ft_ord_qty", "quantity")
                ),
                remaining_qty=pending_qty,
                pending_qty=pending_qty,
                order_id=order_id,
            )
        )
    return orders


def _pending_sell_by_symbol(orders: list[USOpenOrder]) -> dict[str, float]:
    pending: dict[str, float] = {}
    for order in orders:
        if order.side != "sell":
            continue
        pending[order.symbol] = pending.get(order.symbol, 0.0) + order.pending_qty
    return pending


async def _quote_for_symbol(
    quote_service: Any, symbol: str
) -> tuple[float | None, str]:
    if quote_service is None:
        return None, "missing"
    for method_name in ("get_us_quote", "get_quote", "quote"):
        method = getattr(quote_service, method_name, None)
        if not callable(method):
            continue
        quote = await _maybe_await(method(symbol))
        if isinstance(quote, Mapping):
            price = _first_float(
                quote, ("price", "last_price", "lastPrice", "current_price")
            )
            state = str(
                quote.get("state")
                or quote.get("price_state")
                or quote.get("priceState")
                or "live"
            )
        else:
            price = _to_float(
                getattr(quote, "price", None)
                or getattr(quote, "last_price", None)
                or getattr(quote, "current_price", None)
            )
            state = str(
                getattr(quote, "state", None)
                or getattr(quote, "price_state", None)
                or "live"
            )
        if state not in {"live", "stale", "missing"}:
            state = "live" if price is not None else "missing"
        return price, state
    return None, "missing"


async def _build_holding(
    row: Mapping[str, Any],
    *,
    quote_service: Any,
    pending_sell_qty: float,
    warnings: list[str],
) -> USHolding | None:
    raw_symbol = _first_str(row, ("ovrs_pdno", "pdno", "symbol", "ticker"))
    if not raw_symbol:
        warnings.append("kis_live_us_holding_skipped: missing symbol")
        return None
    symbol = to_db_symbol(raw_symbol.upper())
    quantity = (
        _first_float(row, ("ovrs_cblc_qty", "hldg_qty", "quantity", "qty")) or 0.0
    )
    average_cost = _first_float(
        row, ("pchs_avg_pric", "avg_price", "average_cost", "averageCost")
    )
    cost_basis = _first_float(
        row, ("frcr_pchs_amt1", "pchs_amt", "cost_basis", "costBasis")
    )
    if cost_basis is None and average_cost is not None:
        cost_basis = quantity * average_cost

    try:
        quote_price, price_state = await _quote_for_symbol(quote_service, symbol)
    except Exception as exc:
        warnings.append(f"kis_live_us_quote_unavailable:{symbol}: {exc}")
        quote_price = None
        price_state = "missing"

    row_price = _first_float(
        row, ("now_pric2", "now_pric", "last_price", "current_price")
    )
    last_price = quote_price if quote_price is not None else row_price
    if quote_price is None and price_state != "stale":
        price_state = "missing"

    value_usd = quantity * last_price if last_price is not None else None
    pnl_usd = (
        value_usd - cost_basis
        if value_usd is not None and cost_basis is not None
        else None
    )
    pnl_rate = (
        (pnl_usd / cost_basis * 100.0) if pnl_usd is not None and cost_basis else None
    )
    sellable_qty = _first_float(
        row,
        (
            "ord_psbl_qty",
            "sellable_qty",
            "sellableQty",
            "tradable_qty",
            "available_qty",
            "ovrs_cblc_qty",
            "hldg_qty",
            "quantity",
        ),
    )
    if sellable_qty is None:
        sellable_qty = quantity
    pending_qty = min(quantity, pending_sell_qty) if pending_sell_qty > 0 else 0.0

    return USHolding(
        symbol=symbol,
        display_name=_first_str(
            row, ("ovrs_item_name", "name", "display_name", "displayName")
        )
        or symbol,
        quantity=quantity,
        average_cost_usd=average_cost,
        cost_basis_usd=cost_basis,
        last_price_usd=last_price if price_state != "missing" else None,
        value_usd=value_usd if price_state != "missing" else None,
        pnl_usd=pnl_usd if price_state != "missing" else None,
        pnl_rate=pnl_rate if price_state != "missing" else None,
        price_state=price_state,  # type: ignore[arg-type]
        source_of_truth=True,
        is_tradeable=True,
        manual_only=False,
        sellable_qty=max(sellable_qty - pending_qty, 0.0),
        pending_qty=pending_qty,
    )


async def build_kis_us_account_snapshot(
    *,
    kis_client: Any,
    quote_service: Any,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> KISUSAccountSnapshot:
    """Build a read-only KIS-live US account snapshot.

    This function intentionally consumes only KIS account/holding/order inquiry methods.
    Toss/manual holdings are not inputs and therefore cannot become tradeable or sellable.
    """

    warnings: list[str] = []
    usd_cash, usd_buying_power = await _load_margin(kis_client, warnings)
    open_order_rows = await _load_open_orders(kis_client, warnings)
    open_orders = _build_open_orders(open_order_rows)
    pending_sell_qty = _pending_sell_by_symbol(open_orders)
    holding_rows = await _load_holdings(kis_client, warnings)

    holdings: list[USHolding] = []
    for row in holding_rows:
        holding = await _build_holding(
            row,
            quote_service=quote_service,
            pending_sell_qty=pending_sell_qty.get(_order_symbol(row) or "", 0.0),
            warnings=warnings,
        )
        if holding is not None:
            holdings.append(holding)

    return KISUSAccountSnapshot(
        captured_at=now(),
        source="kis_live",
        source_of_truth=True,
        is_tradeable=True,
        manual_only=False,
        usd_cash=usd_cash,
        usd_buying_power=usd_buying_power,
        holdings=holdings,
        open_orders=open_orders,
        warnings=warnings,
    )
