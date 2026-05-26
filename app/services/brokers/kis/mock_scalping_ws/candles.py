"""Tick → 1-minute OHLC candle aggregation (pure, deterministic).

KIS quote WS delivers trade ticks (체결가), not exchange klines, so the
supervisor builds candles itself. ``CandleAggregator.add`` buckets ticks by
minute (derived from an injected ``now`` in epoch seconds) and returns a closed
``Candle`` exactly once, when the first tick of a *later* minute arrives. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.brokers.kis.mock_scalping.signal import Candle

_MINUTE = 60


@dataclass
class CandleAggregator:
    _bucket: int | None = None
    _open: Decimal | None = None
    _high: Decimal | None = None
    _low: Decimal | None = None
    _close: Decimal | None = None

    def add(self, price: Decimal, *, now: float) -> Candle | None:
        """Accumulate a tick. Returns the prior minute's Candle when it closes."""
        bucket = int(now // _MINUTE)
        if self._bucket is None:
            self._start(bucket, price)
            return None
        if bucket == self._bucket:
            assert self._high is not None and self._low is not None
            self._high = max(self._high, price)
            self._low = min(self._low, price)
            self._close = price
            return None
        closed = self._finish()
        self._start(bucket, price)
        return closed

    def _start(self, bucket: int, price: Decimal) -> None:
        self._bucket = bucket
        self._open = self._high = self._low = self._close = price

    def _finish(self) -> Candle:
        assert (
            self._bucket is not None
            and self._open is not None
            and self._high is not None
            and self._low is not None
            and self._close is not None
        )
        return Candle(
            open_time_ms=self._bucket * _MINUTE * 1000,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            close_time_ms=(self._bucket + 1) * _MINUTE * 1000 - 1,
        )
