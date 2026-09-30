"""Immutable value types for the quotes:toss shadow consumer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

StreamMarket = Literal["kr", "us"]
SessionLabel = Literal[
    "nxt_pre", "krx_regular", "nxt_after", "us_pre", "us_regular", "us_after"
]
TickKind = Literal["trade", "orderbook"]

# Stream contract (fillwire README §Sessions): session labels arrive
# verbatim and are never re-classified; they only map to a market bucket.
SESSION_MARKET: dict[str, StreamMarket] = {
    "nxt_pre": "kr",
    "krx_regular": "kr",
    "nxt_after": "kr",
    "us_pre": "us",
    "us_regular": "us",
    "us_after": "us",
}

DROP_REASONS = (
    "unknown_session",
    "bad_symbol",
    "bad_ts",
    "bad_number",
    "empty_tick",
)


@dataclass(frozen=True)
class QuoteTick:
    """One validated ``quotes:toss`` entry."""

    entry_id: str
    symbol: str  # DB-normalized join symbol
    source_symbol: str  # the stream field verbatim
    ts: datetime  # frame time from the entry, tz-aware
    session: str  # verbatim contract label
    market: StreamMarket
    kind: TickKind
    price: Decimal | None  # trade ticks only
    bid1: Decimal | None
    bid_qty: Decimal | None
    ask1: Decimal | None
    ask_qty: Decimal | None


@dataclass(frozen=True)
class DroppedEntry:
    entry_id: str
    reason: str


@dataclass(frozen=True)
class TriggerRow:
    """Pending ``review.quotes_trigger_firings`` row."""

    dedupe_key: str
    trigger_type: str
    outcome: str  # 'fired' | 'not_evaluable'
    symbol: str
    source_symbol: str | None
    market: str | None
    session: str | None
    reference_price: Decimal | None
    current_price: Decimal | None
    window: str
    event_ts: datetime
    kst_date: str
    would_kick: bool
    suppress_reason: str | None
    daily_would_kick_count: int
    last_would_kick_at: datetime | None
    not_evaluable_reason: str | None
    source_ref: str | None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LadderRow:
    """Pending ``review.ladder_touch_events`` row."""

    dedupe_key: str
    order_ledger: str
    order_ledger_id: int
    broker_order_id: str | None
    client_order_id: str | None
    correlation_id: str | None
    event_type: str  # 'approach' | 'touch' | 'fill'
    market: str
    symbol: str
    side: str
    session: str | None
    anchor_price: Decimal
    event_price: Decimal
    distance_pct: Decimal | None
    event_ts: datetime
    received_at: datetime | None
    nxt_tradable: bool | None
    died_at: datetime | None
    stream_entry_id: str | None
    fill_ledger_id: int | None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RungAnchor:
    """One open resting order treated as a ladder rung anchor."""

    ledger_name: str
    ledger_id: int
    symbol: str  # DB symbol as stored on the ledger
    market: str
    side: str
    anchor_price: Decimal
    broker_order_id: str | None
    client_order_id: str | None
    correlation_id: str | None
    received_at: datetime | None
    died_at: datetime | None
    nxt_tradable: bool | None = None  # no stored source in v0


@dataclass(frozen=True)
class OwnFill:
    """One new ``review.execution_ledger`` row for the own_fill trigger."""

    ledger_id: int
    symbol: str
    market: str
    side: str
    price: Decimal
    qty: Decimal
    filled_at: datetime
    broker_order_id: str | None
    broker: str
