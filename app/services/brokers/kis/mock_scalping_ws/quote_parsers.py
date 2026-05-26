"""Pure parser: KIS plaintext quote frame -> typed snapshot. No I/O, no state."""

from __future__ import annotations

from dataclasses import dataclass

from .quote_protocol import (
    DOMESTIC_ORDERBOOK_TR,
    DOMESTIC_TRADE_TR,
    ORDERBOOK_FIELDS,
    QUOTE_TR_CODES,
    TRADE_FIELDS,
)


@dataclass(frozen=True)
class QuoteTick:
    symbol: str
    last_price: float
    ts: str  # HHMMSS as reported by KIS


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_quote_frame(message: str | bytes) -> QuoteTick | OrderBookSnapshot | None:
    """Parse one plaintext KIS real-time quote frame.

    Returns None for: bytes-decode failure, empty/malformed frames, encrypted
    frames (leading '1'), or non-quote TR codes. Read-only; never raises on
    bad input.
    """
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except Exception:
            return None
    message = (message or "").strip()
    if not message:
        return None

    parts = message.split("|")
    if len(parts) < 4:
        return None

    encryption_flag, tr_code, _count, payload = parts[0], parts[1], parts[2], parts[3]
    if encryption_flag != "0":  # quotes are never encrypted
        return None
    if tr_code not in QUOTE_TR_CODES:
        return None

    fields = payload.split("^")

    def at(idx: int) -> str:
        return fields[idx] if idx < len(fields) else ""

    if tr_code == DOMESTIC_TRADE_TR:
        symbol = at(TRADE_FIELDS["symbol"])
        if not symbol:
            return None
        return QuoteTick(
            symbol=symbol,
            last_price=_to_float(at(TRADE_FIELDS["last_price"])),
            ts=at(TRADE_FIELDS["time"]),
        )

    if tr_code == DOMESTIC_ORDERBOOK_TR:
        symbol = at(ORDERBOOK_FIELDS["symbol"])
        if not symbol:
            return None
        return OrderBookSnapshot(
            symbol=symbol,
            bid=_to_float(at(ORDERBOOK_FIELDS["bid"])),
            ask=_to_float(at(ORDERBOOK_FIELDS["ask"])),
            bid_qty=_to_float(at(ORDERBOOK_FIELDS["bid_qty"])),
            ask_qty=_to_float(at(ORDERBOOK_FIELDS["ask_qty"])),
        )

    return None
