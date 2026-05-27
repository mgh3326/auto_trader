"""Tick → 1-minute candle aggregation tests (ROB-321 PR3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping_ws.candles import CandleAggregator


@pytest.mark.unit
def test_first_tick_opens_no_candle() -> None:
    agg = CandleAggregator()
    assert agg.add(Decimal("100"), now=0.0) is None


@pytest.mark.unit
def test_same_minute_ticks_accumulate_ohlc() -> None:
    agg = CandleAggregator()
    assert agg.add(Decimal("100"), now=0.0) is None
    assert agg.add(Decimal("105"), now=10.0) is None  # high
    assert agg.add(Decimal("98"), now=20.0) is None  # low
    # rollover to next minute closes the first candle
    closed = agg.add(Decimal("101"), now=60.0)
    assert closed is not None
    assert closed.open == Decimal("100")
    assert closed.high == Decimal("105")
    assert closed.low == Decimal("98")
    assert closed.close == Decimal("98")  # last price of minute 0


@pytest.mark.unit
def test_rollover_returns_one_candle_per_minute() -> None:
    agg = CandleAggregator()
    agg.add(Decimal("100"), now=0.0)
    c0 = agg.add(Decimal("101"), now=60.0)
    c1 = agg.add(Decimal("102"), now=120.0)
    assert c0 is not None and c0.close == Decimal("100")
    assert c1 is not None and c1.close == Decimal("101")
    assert c0.close_time_ms < c1.close_time_ms
