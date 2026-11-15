"""Pure identity helpers: signal key, correlation id and client order id."""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .constants import CLIENT_ORDER_PREFIX, CORRELATION_PREFIX, LOCKED_LIMITS

_KST = ZoneInfo("Asia/Seoul")
SIDES = ("BUY", "SELL")


def make_signal_key(
    symbol: str, decision_ts: int, side: str, signal_price_text: str
) -> str:
    """``symbol|KST minute|side|decimal close text`` (design §4.2)."""
    if symbol not in LOCKED_LIMITS.universe or side not in SIDES:
        raise ValueError("invalid trend signal identity")
    try:
        price = Decimal(signal_price_text)
    except InvalidOperation as exc:
        raise ValueError("invalid trend signal price") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("invalid trend signal price")
    instant = dt.datetime.fromtimestamp(decision_ts / 1000, tz=dt.UTC)
    minute = instant.astimezone(_KST).strftime("%Y-%m-%d %H:%M")
    return f"{symbol}|{minute}|{side}|{signal_price_text}"


def trend_correlation_id(signal_key: str) -> str:
    return CORRELATION_PREFIX + hashlib.sha256(signal_key.encode()).hexdigest()[:24]


def trend_client_order_id(signal_key: str, leg_key: str) -> str:
    """Deterministic, <=36 chars (Binance ``newClientOrderId`` limit)."""
    digest = hashlib.sha256(f"{signal_key}|{leg_key}".encode()).hexdigest()[:28]
    return CLIENT_ORDER_PREFIX + digest
