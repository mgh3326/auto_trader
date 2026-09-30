"""Validation of ``quotes:toss`` stream entries.

The stream contract (fillwire README §Stream entries) gives every entry
all of ``symbol``, ``ts`` (RFC 3339 ms), ``price``, ``bid1``, ``bid_qty``,
``ask1``, ``ask_qty`` and ``session`` — trade ticks carry ``price`` and
leave the orderbook fields empty, orderbook ticks do the opposite.  The
session label is stored verbatim; anything outside the six contract
labels is dropped and counted, never coerced.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.core.symbol import to_db_symbol

from .types import (
    SESSION_MARKET,
    DroppedEntry,
    QuoteTick,
)


def _decimal_or_none(raw: object) -> Decimal | None:
    """``''``/None means 'not this tick's field'; anything else must parse."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("bad_number") from None
    if not value.is_finite():
        raise ValueError("bad_number")
    return value


def parse_quote_entry(
    entry_id: str, fields: Mapping[str, object]
) -> QuoteTick | DroppedEntry:
    """Validate one stream entry; returns the tick or the drop reason."""

    # Exact match only: the label is used as given; padding/case variants
    # are unknown labels, not reclassifications.
    session = str(fields.get("session") or "")
    market = SESSION_MARKET.get(session)
    if market is None:
        # Unknown labels are dropped + counted, never re-classified (A6).
        return DroppedEntry(entry_id, "unknown_session")

    source_symbol = str(fields.get("symbol") or "").strip()
    if not source_symbol:
        return DroppedEntry(entry_id, "bad_symbol")

    raw_ts = fields.get("ts")
    try:
        ts = datetime.fromisoformat(str(raw_ts))
    except (TypeError, ValueError):
        return DroppedEntry(entry_id, "bad_ts")
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        return DroppedEntry(entry_id, "bad_ts")

    try:
        price = _decimal_or_none(fields.get("price"))
        bid1 = _decimal_or_none(fields.get("bid1"))
        bid_qty = _decimal_or_none(fields.get("bid_qty"))
        ask1 = _decimal_or_none(fields.get("ask1"))
        ask_qty = _decimal_or_none(fields.get("ask_qty"))
    except ValueError:
        return DroppedEntry(entry_id, "bad_number")

    # Non-positive prices are malformed: a 0/-1 trade price would fabricate
    # a -100% spike and a 0-price touch. Quantities may legitimately be 0.
    if (
        (price is not None and price <= 0)
        or (bid1 is not None and bid1 <= 0)
        or (ask1 is not None and ask1 <= 0)
    ):
        return DroppedEntry(entry_id, "bad_number")

    has_book = any(v is not None for v in (bid1, bid_qty, ask1, ask_qty))
    if price is None and not has_book:
        return DroppedEntry(entry_id, "empty_tick")
    # Trade ticks leave book fields empty; orderbook ticks leave price
    # empty. Both populated is malformed, not a trade tick with extras.
    if price is not None and has_book:
        return DroppedEntry(entry_id, "mixed_fields")

    kind = "trade" if price is not None else "orderbook"
    return QuoteTick(
        entry_id=entry_id,
        symbol=to_db_symbol(source_symbol),
        source_symbol=source_symbol,
        ts=ts,
        session=session,
        market=market,
        kind=kind,
        price=price,
        bid1=bid1,
        bid_qty=bid_qty,
        ask1=ask1,
        ask_qty=ask_qty,
    )
