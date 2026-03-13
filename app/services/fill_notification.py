"""Fill notification schema, normalization, and message formatting."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

_US_SYMBOL_RESERVED_TOKENS = {
    "PROD",
    "RESERVED",
    "ENV",
    "HTS",
    "NASD",
    "NASDAQ",
    "NYSE",
    "AMEX",
    "KRX",
}


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
    fill_status: str | None = None
    market_type: str | None = None
    currency: str | None = None

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


def _normalize_fill_status(value: Any) -> str | None:
    text = _safe_text_or_none(value)
    if text is None:
        return None
    normalized = text.lower()
    if normalized in {"filled", "partial"}:
        return normalized
    return None


def _normalize_market_type(value: Any) -> str | None:
    text = _safe_text_or_none(value)
    if text is None:
        return None

    normalized = text.lower()
    aliases = {
        "kr": "kr",
        "krx": "kr",
        "korea": "kr",
        "korean": "kr",
        "domestic": "kr",
        "equity_kr": "kr",
        "국내주식": "kr",
        "300": "kr",
        "us": "us",
        "usa": "us",
        "overseas": "us",
        "equity_us": "us",
        "해외주식": "us",
        "nas": "us",
        "nasd": "us",
        "nasdaq": "us",
        "nys": "us",
        "nyse": "us",
        "ams": "us",
        "amex": "us",
        "512": "us",
        "513": "us",
        "529": "us",
        "crypto": "crypto",
        "cryptocurrency": "crypto",
        "coin": "crypto",
        "암호화폐": "crypto",
    }
    return aliases.get(normalized)


def _normalize_currency(value: Any) -> str | None:
    text = _safe_text_or_none(value)
    if text is None:
        return None

    normalized = text.upper()
    aliases = {
        "USD": "USD",
        "$": "USD",
        "US DOLLAR": "USD",
        "KRW": "KRW",
        "WON": "KRW",
        "원": "KRW",
        "KRW원": "KRW",
    }
    return aliases.get(normalized, normalized if normalized in {"USD", "KRW"} else None)


def _default_currency_for_market(
    market_type: str | None, *, account: str | None = None
) -> str | None:
    if market_type == "us":
        return "USD"
    if market_type in {"kr", "crypto"}:
        return "KRW"
    normalized_account = _safe_text_or_none(account)
    if normalized_account is not None and normalized_account.lower() == "upbit":
        return "KRW"
    return None


def _resolve_fill_currency(
    raw: Mapping[str, Any], *, market_type: str | None, account: str | None = None
) -> str | None:
    explicit_currency = _normalize_currency(
        _pick_first(raw, ["currency", "currency_code", "settlement_currency"])
    )
    if explicit_currency is not None:
        return explicit_currency
    return _default_currency_for_market(market_type, account=account)


def _looks_like_kr_symbol(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6


def _looks_like_crypto_symbol(symbol: str) -> bool:
    normalized = symbol.strip().upper()
    return normalized.startswith("KRW-") or normalized.startswith("USDT-")


def _looks_like_us_symbol(symbol: str) -> bool:
    normalized = symbol.strip()
    if not normalized or normalized != normalized.upper():
        return False
    if normalized == "UNKNOWN":
        return False
    if _looks_like_crypto_symbol(normalized):
        return False
    cleaned = normalized.replace(".", "").replace("-", "").replace("/", "")
    if not cleaned or not cleaned[0].isalpha():
        return False
    if cleaned in _US_SYMBOL_RESERVED_TOKENS:
        return False
    if cleaned.startswith(("ORDER", "ACNT", "ACCOUNT", "CUST", "USER")):
        return False
    if any(ch.isdigit() for ch in cleaned) and len(cleaned) >= 8:
        return False
    return bool(cleaned) and cleaned.isalnum() and 1 <= len(cleaned) <= 10


def _resolve_fill_market_type(
    raw: Mapping[str, Any], *, symbol: str, account: str | None = None
) -> str | None:
    explicit_market = _normalize_market_type(
        _pick_first(raw, ["market_type", "market", "ovrs_excg_cd", "prdt_type_cd"])
    )
    if explicit_market is not None:
        return explicit_market
    normalized_account = _safe_text_or_none(account or raw.get("account"))
    if normalized_account is not None and normalized_account.lower() == "upbit":
        return "crypto"
    if _looks_like_kr_symbol(symbol):
        return "kr"
    if _looks_like_us_symbol(symbol):
        return "us"
    return None


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
        market_type = order.market_type or _resolve_fill_market_type(
            order.to_dict(), symbol=order.symbol, account=order.account
        )
        currency = _normalize_currency(order.currency) or _resolve_fill_currency(
            order.to_dict(), market_type=market_type, account=order.account
        )
        if market_type == order.market_type and currency == order.currency:
            return order
        return replace(order, market_type=market_type, currency=currency)

    symbol = str(order.get("symbol") or "UNKNOWN")
    account = str(order.get("account") or "unknown")
    market_type = _resolve_fill_market_type(order, symbol=symbol, account=account)
    currency = _resolve_fill_currency(order, market_type=market_type, account=account)

    return FillOrder(
        symbol=symbol,
        side=_normalize_side(str(order.get("side") or "")),
        filled_price=_safe_float(order.get("filled_price")),
        filled_qty=_safe_float(order.get("filled_qty")),
        filled_amount=_safe_float(order.get("filled_amount")),
        filled_at=_parse_timestamp(order.get("filled_at")),
        account=account,
        order_price=_safe_float_or_none(order.get("order_price")),
        order_id=_safe_text_or_none(order.get("order_id")),
        order_type=_safe_text_or_none(order.get("order_type")),
        fill_status=_normalize_fill_status(
            _pick_first(order, ["fill_status", "execution_status"])
        ),
        market_type=market_type,
        currency=currency,
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
        fill_status="filled",
        market_type="crypto",
        currency="KRW",
    )


def normalize_kis_fill(raw: Mapping[str, Any]) -> FillOrder:
    symbol = str(_pick_first(raw, ["symbol", "pdno", "mksc_shrn_iscd"]) or "UNKNOWN")
    account = str(_pick_first(raw, ["account"]) or "kis")
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

    market_type = _resolve_fill_market_type(raw, symbol=symbol, account=account)
    currency = _resolve_fill_currency(raw, market_type=market_type, account=account)

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
        account=account,
        order_price=_safe_float_or_none(
            _pick_first(raw, ["order_price", "ord_unpr", "ft_ord_unpr3"])
        ),
        order_id=_safe_text_or_none(
            _pick_first(raw, ["order_id", "ord_no", "odno", "orgn_ord_no"])
        ),
        order_type=_safe_text_or_none(_pick_first(raw, ["order_type", "ord_dvsn"])),
        fill_status=_normalize_fill_status(
            _pick_first(raw, ["fill_status", "execution_status"])
        ),
        market_type=market_type,
        currency=currency,
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


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


def _format_money(value: float, currency: str | None) -> str:
    if currency == "USD":
        return _format_usd(value)
    return _format_krw(value)


def _format_quantity(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 1e-12:
        return str(int(rounded))
    return f"{value:.12f}".rstrip("0").rstrip(".")


def format_fill_message(order: FillOrderLike) -> str:
    normalized = coerce_fill_order(order)
    side_emoji = _format_side_emoji(normalized.side)
    side_text = _format_side_text(normalized.side)
    is_partial = normalized.fill_status == "partial"
    fill_label = "부분체결" if is_partial else "체결"

    price_diff = ""
    if normalized.order_price and normalized.order_price != 0:
        diff_pct = (
            (normalized.filled_price - normalized.order_price) / normalized.order_price
        ) * 100
        price_diff = f" ({diff_pct:+.2f}%)"

    message = (
        f"{side_emoji} 체결 알림\n\n"
        f"종목: {normalized.symbol}\n"
        f"구분: {side_text} {fill_label}\n"
        f"체결가: {_format_money(normalized.filled_price, normalized.currency)}{price_diff}\n"
        f"수량: {_format_quantity(normalized.filled_qty)}\n"
        f"금액: {_format_money(normalized.filled_amount, normalized.currency)}\n"
        f"시간: {normalized.filled_at}\n\n"
        f"계좌: {normalized.account}"
    )
    if normalized.order_id:
        message += f"\n주문: {normalized.order_id[:8]}..."
    return message
