"""Live read-only current open-order service for /invest (ROB-572)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.schemas.open_orders import OpenOrderMarket, OpenOrderRow

_KST = dt.timezone(dt.timedelta(hours=9), name="KST")
_KIS_SIDE_BUY = {"02", "buy", "b", "매수"}
_KIS_SIDE_SELL = {"01", "sell", "s", "매도"}


def _first_str(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_kis_ordered_at(row: dict[str, Any]) -> dt.datetime | None:
    explicit = _parse_datetime(row.get("ordered_at") or row.get("placed_at"))
    if explicit is not None:
        return explicit
    ord_dt = row.get("ord_dt")
    ord_tmd = row.get("ord_tmd")
    if not ord_dt or not ord_tmd:
        return None
    try:
        return dt.datetime.strptime(f"{ord_dt}{ord_tmd}", "%Y%m%d%H%M%S").replace(
            tzinfo=_KST
        )
    except ValueError:
        return None


def _parse_datetime(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def _kis_side(row: dict[str, Any]) -> Literal["buy", "sell", "unknown"]:
    raw = (
        str(
            row.get("sll_buy_dvsn_cd")
            or row.get("sll_buy_dvsn_cd_name")
            or row.get("side")
            or ""
        )
        .strip()
        .lower()
    )
    if raw in _KIS_SIDE_BUY:
        return "buy"
    if raw in _KIS_SIDE_SELL:
        return "sell"
    return "unknown"


def normalize_kis_order(
    row: dict[str, Any],
    *,
    market: Literal["kr", "us"],
    exchange: str,
) -> OpenOrderRow:
    order_no = _first_str(row, ("ord_no", "odno", "order_id")) or "unknown"
    symbol = _first_str(row, ("pdno", "symbol", "ticker")) or "unknown"
    quantity = _decimal(_first_str(row, ("ord_qty", "ft_ord_qty", "quantity", "qty")))
    remaining = _decimal(
        _first_str(row, ("nccs_qty", "rmn_qty", "remaining_qty", "remaining_quantity"))
    )
    if remaining is None:
        remaining = quantity
    status = _first_str(row, ("prcs_stat_name", "status", "raw_status")) or "pending"

    return OpenOrderRow(
        broker="kis",
        market=market,
        symbol=symbol.upper() if market == "us" else symbol,
        symbol_name=_first_str(row, ("prdt_name", "symbol_name", "name")),
        side=_kis_side(row),
        order_type=_first_str(row, ("ord_dvsn_name", "ord_dvsn", "order_type")),
        time_in_force=None,
        price=_decimal(_first_str(row, ("ord_unpr", "ft_ord_unpr3", "ord_unpr3", "price"))),
        quantity=quantity,
        remaining_qty=remaining,
        filled_qty=_decimal(_first_str(row, ("ft_ccld_qty", "ccld_qty", "filled_qty"))),
        status="pending",
        raw_status=status,
        ordered_at=_parse_kis_ordered_at(row),
        order_no=order_no,
        exchange=exchange,
        currency="KRW" if market == "kr" else "USD",
    )


def normalize_upbit_order(row: dict[str, Any]) -> OpenOrderRow:
    side_raw = str(row.get("side") or "").strip().lower()
    side: Literal["buy", "sell", "unknown"]
    if side_raw == "bid":
        side = "buy"
    elif side_raw == "ask":
        side = "sell"
    else:
        side = "unknown"
    symbol = str(row.get("market") or "unknown").strip().upper()
    quote = symbol.split("-", 1)[0] if "-" in symbol else "KRW"
    return OpenOrderRow(
        broker="upbit",
        market="crypto",
        symbol=symbol,
        symbol_name=None,
        side=side,
        order_type=_first_str(row, ("ord_type", "order_type")),
        time_in_force=None,
        price=_decimal(row.get("price")),
        quantity=_decimal(row.get("volume")),
        remaining_qty=_decimal(row.get("remaining_volume")),
        filled_qty=_decimal(row.get("executed_volume")),
        status="pending",
        raw_status=_first_str(row, ("state", "status")) or "wait",
        ordered_at=_parse_datetime(row.get("created_at") or row.get("ordered_at")),
        order_no=str(row.get("uuid") or "unknown"),
        exchange="UPBIT",
        currency=quote,
    )
