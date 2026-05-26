"""Per-symbol market state built from quote/orderbook frames.

Pure in-memory tracker. The caller injects ``now`` (monotonic seconds) so
freshness is deterministic and testable; this module performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from .quote_parsers import OrderBookSnapshot, QuoteTick


@dataclass
class MarketState:
    symbol: str
    last_price: float | None = None
    last_ts: str | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    _updated_at: float | None = None

    def update_from_tick(self, tick: QuoteTick, *, now: float) -> None:
        self.last_price = tick.last_price
        self.last_ts = tick.ts
        self._updated_at = now

    def update_from_book(self, book: OrderBookSnapshot, *, now: float) -> None:
        self.bid = book.bid
        self.ask = book.ask
        self.bid_qty = book.bid_qty
        self.ask_qty = book.ask_qty
        self._updated_at = now

    def spread_bps(self) -> float | None:
        """(ask - bid) / mid in basis points. None until a valid book is seen."""
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return (self.ask - self.bid) / mid * 10_000

    def age_seconds(self, *, now: float) -> float | None:
        """Seconds since the most recent tick or book update. None if never updated."""
        if self._updated_at is None:
            return None
        return now - self._updated_at
