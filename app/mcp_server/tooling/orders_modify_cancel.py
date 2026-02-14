"""Order modify/cancel normalization helpers."""

from __future__ import annotations

import hashlib
from typing import Any

from app.mcp_server.tooling.shared import logger


def _map_upbit_state(state: str, filled: float, remaining: float) -> str:
    if state == "wait":
        return "pending"
    if state == "done":
        if filled > 0:
            return "filled"
        return "cancelled"
    if state == "cancelled":
        return "cancelled"
    return "partial"


def _normalize_upbit_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = order.get("side", "")
    side = "buy" if side_code == "bid" else "sell"

    state = order.get("state", "")
    remaining = float(order.get("remaining_volume", 0) or 0)
    filled = float(order.get("executed_volume", 0) or 0)
    ordered = remaining + filled

    ordered_price = float(order.get("price", 0) or 0)
    filled_price = float(order.get("avg_price", 0) or 0)
    status = _map_upbit_state(state, filled, remaining)

    return {
        "order_id": order.get("uuid", ""),
        "symbol": order.get("market", ""),
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": order.get("created_at", ""),
        "filled_at": order.get("done_at", ""),
        "currency": "KRW",
    }


def _get_kis_field(order: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = order.get(key)
        if value:
            return value
    return default


def _extract_kis_order_number(order: dict[str, Any]) -> str:
    value = _get_kis_field(
        order,
        "odno",
        "ODNO",
        "ord_no",
        "ORD_NO",
        "orgn_odno",
        "ORGN_ODNO",
        default="",
    )
    if value is None:
        return ""
    return str(value).strip()


def _build_temp_kr_order_id(
    *,
    symbol: str,
    side: str,
    ordered_price: int,
    ordered_qty: int,
    ordered_at: str,
) -> str:
    raw = "|".join(
        [symbol, side, str(ordered_price), str(ordered_qty), ordered_at.strip()]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()
    return f"TEMP_KR_{digest}"


def _map_kis_status(filled: int, remaining: int, status_name: str) -> str:
    if status_name in ("접수", "주문접수"):
        return "pending"
    if status_name == "주문취소":
        return "cancelled"
    if status_name in ("체결", "미체결"):
        if remaining > 0:
            return "partial"
        return "filled"
    return "pending"


def _normalize_kis_domestic_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    side = "buy" if side_code == "02" else "sell"

    ordered = int(float(_get_kis_field(order, "ord_qty", "ORD_QTY", default=0) or 0))
    filled = int(float(_get_kis_field(order, "ccld_qty", "CCLD_QTY", default=0) or 0))
    remaining = ordered - filled

    ordered_price = int(
        float(_get_kis_field(order, "ord_unpr", "ORD_UNPR", default=0) or 0)
    )
    filled_price = int(
        float(_get_kis_field(order, "ccld_unpr", "CCLD_UNPR", default=0) or 0)
    )

    status = _map_kis_status(
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
    )
    symbol = str(_get_kis_field(order, "pdno", "PDNO"))
    ordered_at = (
        f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
        f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
    )
    order_id = _extract_kis_order_number(order)
    if not order_id:
        order_id = _build_temp_kr_order_id(
            symbol=symbol,
            side=side,
            ordered_price=ordered_price,
            ordered_qty=ordered,
            ordered_at=ordered_at,
        )
        logger.warning(
            "Missing order_id for KR order (symbol=%s, side=%s, qty=%s, price=%s, ordered_at=%s), generated %s",
            symbol,
            side,
            ordered,
            ordered_price,
            ordered_at,
            order_id,
        )

    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "KRW",
    }


def _normalize_kis_overseas_order(order: dict[str, Any]) -> dict[str, Any]:
    side_code = _get_kis_field(order, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    side = "buy" if side_code == "02" else "sell"

    ordered = int(
        float(_get_kis_field(order, "ft_ord_qty", "FT_ORD_QTY", default=0) or 0)
    )
    filled = int(
        float(_get_kis_field(order, "ft_ccld_qty", "FT_CCLD_QTY", default=0) or 0)
    )
    remaining = ordered - filled

    ordered_price = float(
        _get_kis_field(order, "ft_ord_unpr3", "FT_ORD_UNPR3", default=0) or 0
    )
    filled_price = float(
        _get_kis_field(order, "ft_ccld_unpr3", "FT_CCLD_UNPR3", default=0) or 0
    )

    status = _map_kis_status(
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
    )

    return {
        "order_id": _extract_kis_order_number(order),
        "symbol": _get_kis_field(order, "pdno", "PDNO"),
        "side": side,
        "status": status,
        "ordered_qty": ordered,
        "filled_qty": filled,
        "remaining_qty": remaining,
        "ordered_price": ordered_price,
        "filled_avg_price": filled_price,
        "ordered_at": (
            f"{_get_kis_field(order, 'ord_dt', 'ORD_DT')} "
            f"{_get_kis_field(order, 'ord_tmd', 'ORD_TMD')}"
        ),
        "filled_at": "",
        "currency": "USD",
    }


__all__ = [
    "_extract_kis_order_number",
    "_get_kis_field",
    "_normalize_kis_domestic_order",
    "_normalize_kis_overseas_order",
    "_normalize_upbit_order",
]
