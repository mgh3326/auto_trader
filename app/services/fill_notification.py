"""Fill notification schema, normalization, and message formatting."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FillOrder:
    symbol: str
    side: str
    filled_price: float
    filled_qty: float
    filled_amount: float
    filled_at: str
    account: str
    order_price: float | None = None
    order_id: str | None = None
    order_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FillOrderLike = FillOrder | Mapping[str, Any]


def _pick_first(payload: Mapping[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_float_or_none(value: Any) -> float | None:
    parsed = _safe_float(value, default=0.0)
    if parsed == 0:
        return None
    return parsed


def _safe_text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_side(value: str | None) -> str:
    if not value:
        return "unknown"
    normalized = value.strip().upper()
    buy_values = {"BID", "BUY", "B", "02", "2", "매수"}
    sell_values = {"ASK", "SELL", "S", "01", "1", "매도"}
    if normalized in buy_values:
        return "bid"
    if normalized in sell_values:
        return "ask"
    return "unknown"


def _parse_timestamp(value: Any) -> str:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    if isinstance(value, (int, float, Decimal)):
        numeric = float(value)
        if numeric > 1e12:
            numeric /= 1000
        return (
            datetime.fromtimestamp(numeric, tz=UTC).replace(microsecond=0).isoformat()
        )

    text = str(value).strip()
    if not text:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    if "T" in text:
        return text
    if text.isdigit():
        if len(text) == 14:
            return datetime.strptime(text, "%Y%m%d%H%M%S").isoformat()
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").isoformat()
        if len(text) == 6:
            today = datetime.now(UTC).strftime("%Y%m%d")
            return datetime.strptime(today + text, "%Y%m%d%H%M%S").isoformat()
        return _parse_timestamp(float(text))
    return text


def coerce_fill_order(order: FillOrderLike) -> FillOrder:
    if isinstance(order, FillOrder):
        return order

    return FillOrder(
        symbol=str(order.get("symbol") or "UNKNOWN"),
        side=_normalize_side(str(order.get("side") or "")),
        filled_price=_safe_float(order.get("filled_price")),
        filled_qty=_safe_float(order.get("filled_qty")),
        filled_amount=_safe_float(order.get("filled_amount")),
        filled_at=_parse_timestamp(order.get("filled_at")),
        account=str(order.get("account") or "unknown"),
        order_price=_safe_float_or_none(order.get("order_price")),
        order_id=_safe_text_or_none(order.get("order_id")),
        order_type=_safe_text_or_none(order.get("order_type")),
    )


def normalize_upbit_fill(raw: Mapping[str, Any]) -> FillOrder:
    symbol = str(_pick_first(raw, ["code", "market", "symbol"]) or "UNKNOWN")
    side = _normalize_side(str(_pick_first(raw, ["ask_bid", "side"]) or ""))
    filled_price = _safe_float(_pick_first(raw, ["trade_price", "price", "avg_price"]))
    filled_qty = _safe_float(
        _pick_first(raw, ["trade_volume", "executed_volume", "volume"])
    )
    filled_amount = _safe_float(_pick_first(raw, ["trade_amount", "executed_amount"]))
    if filled_amount <= 0 and filled_price > 0 and filled_qty > 0:
        filled_amount = filled_price * filled_qty

    return FillOrder(
        symbol=symbol,
        side=side,
        filled_price=filled_price,
        filled_qty=filled_qty,
        filled_amount=filled_amount,
        filled_at=_parse_timestamp(
            _pick_first(
                raw,
                [
                    "trade_timestamp",
                    "trade_time",
                    "filled_at",
                    "created_at",
                    "timestamp",
                ],
            )
        ),
        account="upbit",
        order_price=_safe_float_or_none(_pick_first(raw, ["order_price", "price"])),
        order_id=_safe_text_or_none(_pick_first(raw, ["uuid", "order_id"])),
        order_type=_safe_text_or_none(_pick_first(raw, ["order_type", "ord_type"])),
    )


def normalize_kis_fill(raw: Mapping[str, Any]) -> FillOrder:
    symbol = str(_pick_first(raw, ["symbol", "pdno", "mksc_shrn_iscd"]) or "UNKNOWN")
    side = _normalize_side(
        str(_pick_first(raw, ["side", "sll_buy_dvsn_cd", "buy_sell", "bsop_gb"]) or "")
    )
    filled_price = _safe_float(
        _pick_first(raw, ["filled_price", "ccld_unpr", "ft_ccld_unpr3", "price"])
    )
    filled_qty = _safe_float(
        _pick_first(raw, ["filled_qty", "ccld_qty", "ft_ccld_qty", "qty"])
    )
    filled_amount = _safe_float(
        _pick_first(raw, ["filled_amount", "ccld_amt", "ft_ccld_amt3", "amount"])
    )
    if filled_amount <= 0 and filled_price > 0 and filled_qty > 0:
        filled_amount = filled_price * filled_qty

    if filled_price == 0 or filled_qty == 0:
        logger.debug("KIS best-effort fill normalization fallback: raw=%s", raw)

    return FillOrder(
        symbol=symbol,
        side=side,
        filled_price=filled_price,
        filled_qty=filled_qty,
        filled_amount=filled_amount,
        filled_at=_parse_timestamp(
            _pick_first(
                raw, ["filled_at", "timestamp", "exec_time", "ord_tmd", "ccld_time"]
            )
        ),
        account=str(_pick_first(raw, ["account"]) or "kis"),
        order_price=_safe_float_or_none(
            _pick_first(raw, ["order_price", "ord_unpr", "ft_ord_unpr3"])
        ),
        order_id=_safe_text_or_none(
            _pick_first(raw, ["order_id", "ord_no", "odno", "orgn_ord_no"])
        ),
        order_type=_safe_text_or_none(_pick_first(raw, ["order_type", "ord_dvsn"])),
    )


def _format_side_emoji(side: str) -> str:
    if side == "bid":
        return "🟢"
    if side == "ask":
        return "🔴"
    return "⚪"


def _format_side_text(side: str) -> str:
    if side == "bid":
        return "매수"
    if side == "ask":
        return "매도"
    return "미확인"


def _format_krw(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return f"{int(rounded):,}원"
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return f"{text}원"


def _format_quantity(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 1e-12:
        return str(int(rounded))
    return f"{value:.12f}".rstrip("0").rstrip(".")


def format_fill_message(order: FillOrderLike) -> str:
    normalized = coerce_fill_order(order)
    side_emoji = _format_side_emoji(normalized.side)
    side_text = _format_side_text(normalized.side)

    price_diff = ""
    if normalized.order_price and normalized.order_price != 0:
        diff_pct = (
            (normalized.filled_price - normalized.order_price) / normalized.order_price
        ) * 100
        price_diff = f" ({diff_pct:+.2f}%)"

    message = (
        f"{side_emoji} 체결 알림\n\n"
        f"종목: {normalized.symbol}\n"
        f"구분: {side_text} 체결\n"
        f"체결가: {_format_krw(normalized.filled_price)}{price_diff}\n"
        f"수량: {_format_quantity(normalized.filled_qty)}\n"
        f"금액: {_format_krw(normalized.filled_amount)}\n"
        f"시간: {normalized.filled_at}\n\n"
        f"계좌: {normalized.account}"
    )
    if normalized.order_id:
        message += f"\n주문: {normalized.order_id[:8]}..."
    return message
