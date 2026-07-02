# tests/test_forecast_service_pure.py
"""ROB-650 — pure (DB-free) unit tests for Brier + price-target classification."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal.forecast_service import (
    ForecastValidationError,
    brier_score,
    classify_price_target_outcome,
)

pytestmark = pytest.mark.unit


def _candle(*, high: float, low: float, t: datetime | None = None) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=t or datetime(2026, 6, 3, tzinfo=UTC),
        symbol="TEST",
        partition="KRX",
        open=(high + low) / 2,
        high=high,
        low=low,
        close=(high + low) / 2,
        adj_close=None,
        volume=1000.0,
        value=1000.0,
        source="kis",
    )


@pytest.mark.parametrize(
    ("probability", "outcome", "expected"),
    [
        (0.0, False, 0.0),
        (0.0, True, 1.0),
        (0.5, True, 0.25),
        (0.5, False, 0.25),
        (1.0, True, 0.0),
        (1.0, False, 1.0),
    ],
)
def test_brier_score_boundaries(probability, outcome, expected):
    assert brier_score(probability, outcome) == pytest.approx(expected)


def test_classify_at_or_above_hit():
    candles = [
        _candle(high=100.0, low=95.0),
        _candle(high=131.0, low=120.0),  # touches target
        _candle(high=110.0, low=105.0),
    ]
    hit, extreme = classify_price_target_outcome(
        candles, direction="at_or_above", target_price=130.0
    )
    assert hit is True
    assert extreme == pytest.approx(131.0)


def test_classify_at_or_above_miss():
    candles = [_candle(high=100.0, low=95.0), _candle(high=120.0, low=110.0)]
    hit, extreme = classify_price_target_outcome(
        candles, direction="at_or_above", target_price=130.0
    )
    assert hit is False
    assert extreme == pytest.approx(120.0)  # max high observed


def test_classify_at_or_below_hit():
    candles = [_candle(high=100.0, low=95.0), _candle(high=90.0, low=79.0)]
    hit, extreme = classify_price_target_outcome(
        candles, direction="at_or_below", target_price=80.0
    )
    assert hit is True
    assert extreme == pytest.approx(79.0)  # min low observed


def test_classify_at_or_below_miss():
    candles = [_candle(high=100.0, low=95.0), _candle(high=98.0, low=92.0)]
    hit, extreme = classify_price_target_outcome(
        candles, direction="at_or_below", target_price=80.0
    )
    assert hit is False
    assert extreme == pytest.approx(92.0)


def test_classify_empty_raises():
    with pytest.raises(ForecastValidationError):
        classify_price_target_outcome([], direction="at_or_above", target_price=1.0)


def test_classify_invalid_direction_raises():
    with pytest.raises(ForecastValidationError):
        classify_price_target_outcome(
            [_candle(high=1.0, low=0.5)], direction="sideways", target_price=1.0
        )
