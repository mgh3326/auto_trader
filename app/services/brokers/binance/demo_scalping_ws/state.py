"""ROB-317 — per-symbol in-memory market state + freshness.

Pure data structures: no network, no broker, no DB. The supervisor (slice 3)
mutates these from decoded WS events; the trigger (slice 3) reads them.
Freshness is measured from the last event RECEIVED — a half-dead socket can
stay "open" while delivering nothing, so connection liveness is not a
freshness signal. See ROB-317 design §5.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True)
class MarketState:
    """Latest quote/trade for one symbol, with per-stream receipt timestamps."""

    symbol: str
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_trade_price: Decimal | None = None
    book_ticker_at: dt.datetime | None = None
    agg_trade_at: dt.datetime | None = None

    def last_event_at(self) -> dt.datetime | None:
        """Most recent receipt across all streams, or None if no data yet."""
        stamps = [t for t in (self.book_ticker_at, self.agg_trade_at) if t is not None]
        return max(stamps) if stamps else None

    def is_stale(self, *, now: dt.datetime, max_age_seconds: float) -> bool:
        """True when no event arrived within ``max_age_seconds`` (or ever)."""
        last = self.last_event_at()
        if last is None:
            return True
        return (now - last).total_seconds() > max_age_seconds
