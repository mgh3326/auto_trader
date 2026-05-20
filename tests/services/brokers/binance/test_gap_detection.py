"""ROB-285 — Gap detector unit tests.

Pure function — no I/O. Covers the four scenarios in the plan plus the
``last_closed=None`` cold-start path.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.brokers.binance.backfill import (
    BackfillCaps,
    RestBackfiller,
)
from app.services.brokers.binance.dto import BinanceKlineRow
from app.services.brokers.binance.errors import BinanceBackfillCapExceeded
from app.services.brokers.binance.gap_detector import detect_gap


def test_gap_detector_returns_no_gap_when_last_closed_is_none() -> None:
    decision = detect_gap(last_closed=None, interval="1m")
    assert decision.needs_fill is False
    assert decision.since is None
    assert decision.expected_count == 0


def test_gap_detector_returns_no_gap_when_recent_closed_candle() -> None:
    """last_closed = now - 30s, interval = 60s → no full bucket has
    closed since; no fill needed."""
    now = dt.datetime(2026, 5, 20, 12, 0, 30, tzinfo=dt.UTC)
    last_closed = now - dt.timedelta(seconds=30)
    decision = detect_gap(last_closed=last_closed, interval="1m", now=now)
    assert decision.needs_fill is False
    assert decision.expected_count == 0


def test_gap_detector_returns_gap_when_minutes_missing() -> None:
    """last_closed = now - 5m, interval = 60s → 4 completed buckets in
    between (current minute is in-progress and excluded)."""
    now = dt.datetime(2026, 5, 20, 12, 5, 0, tzinfo=dt.UTC)
    last_closed = now - dt.timedelta(minutes=5)
    decision = detect_gap(last_closed=last_closed, interval="1m", now=now)
    assert decision.needs_fill is True
    assert decision.expected_count == 4
    assert decision.since == last_closed + dt.timedelta(minutes=1)


@pytest.mark.asyncio
async def test_gap_within_cap_triggers_rest_backfill_returns_klines() -> None:
    """When the gap is within cap, RestBackfiller returns the candles
    (the orchestration layer is responsible for persisting them)."""

    # Fake REST: returns one kline per minute since ``since``.
    def _mk(t: dt.datetime) -> BinanceKlineRow:
        from decimal import Decimal

        return BinanceKlineRow(
            symbol="BTCUSDT",
            interval="1m",
            open_time=t,
            close_time=t + dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            base_volume=Decimal("0"),
            quote_volume=None,
            trade_count=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
            is_closed=True,
        )

    class _FakeRest:
        def __init__(self) -> None:
            self.calls = 0

        async def klines(
            self,
            symbol: str,
            interval: str,
            *,
            start_time: dt.datetime,
            end_time: dt.datetime | None = None,
            limit: int,
        ) -> list[BinanceKlineRow]:
            self.calls += 1
            return [
                _mk(start_time + dt.timedelta(minutes=i)) for i in range(100)
            ]

    now = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    last_closed = now - dt.timedelta(minutes=100)
    decision = detect_gap(last_closed=last_closed, interval="1m", now=now)
    assert decision.needs_fill is True

    rest = _FakeRest()
    bf = RestBackfiller(
        rest=rest,
        caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    assert decision.since is not None
    result = await bf.backfill(
        symbol="BTCUSDT", interval="1m", since=decision.since
    )
    # 100 klines returned in one page (page_size=1000 > 100).
    assert len(result.klines) == 100


@pytest.mark.asyncio
async def test_gap_beyond_cap_raises_backfill_cap_exceeded() -> None:
    """A simulated 10_000-candle gap exceeds the default 5000-cap and
    triggers BinanceBackfillCapExceeded — the orchestration layer is
    responsible for flipping the instrument to manual_backfill_required."""

    def _mk(t: dt.datetime) -> BinanceKlineRow:
        from decimal import Decimal

        return BinanceKlineRow(
            symbol="BTCUSDT",
            interval="1m",
            open_time=t,
            close_time=t + dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            base_volume=Decimal("0"),
            quote_volume=None,
            trade_count=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
            is_closed=True,
        )

    class _FakeRest:
        async def klines(
            self,
            symbol: str,
            interval: str,
            *,
            start_time: dt.datetime,
            end_time: dt.datetime | None = None,
            limit: int,
        ) -> list[BinanceKlineRow]:
            return [
                _mk(start_time + dt.timedelta(minutes=i)) for i in range(limit)
            ]

    rest = _FakeRest()
    bf = RestBackfiller(
        rest=rest,
        caps=BackfillCaps(max_candles=5000, max_requests=10, page_size=1000),
    )
    since = dt.datetime(2026, 5, 1, tzinfo=dt.UTC)
    with pytest.raises(BinanceBackfillCapExceeded):
        await bf.backfill(symbol="BTCUSDT", interval="1m", since=since)
