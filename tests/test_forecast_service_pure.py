# tests/test_forecast_service_pure.py
"""ROB-650 — pure (DB-free) unit tests for Brier + price-target classification."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal import forecast_service as svc
from app.services.trade_journal.forecast_service import (
    ForecastValidationError,
    TerminalCloseDataError,
    brier_score,
    classify_price_target_outcome,
    classify_terminal_close_outcome,
)

pytestmark = pytest.mark.unit


def _candle(
    *,
    high: float,
    low: float,
    close: float | None = None,
    source: str = "kis",
    t: datetime | None = None,
) -> DailyCandleRow:
    return DailyCandleRow(
        time_utc=t or datetime(2026, 6, 3, tzinfo=UTC),
        symbol="TEST",
        partition="KRX",
        open=(high + low) / 2,
        high=high,
        low=low,
        close=close if close is not None else (high + low) / 2,
        adj_close=None,
        volume=1000.0,
        value=1000.0,
        source=source,
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


def test_terminal_close_up_ignores_window_high_touch():
    candles = [
        _candle(
            high=140.0,
            low=120.0,
            close=125.0,
            t=datetime(2026, 6, 4, tzinfo=UTC),
        ),
        _candle(
            high=135.0,
            low=119.0,
            close=129.0,
            t=datetime(2026, 6, 5, tzinfo=UTC),
        ),
    ]

    outcome, observed, selected = classify_terminal_close_outcome(
        candles,
        review_date=date(2026, 6, 5),
        direction="up",
        target_price=130.0,
    )

    assert outcome is False
    assert observed == pytest.approx(129.0)
    assert selected.time_utc.date() == date(2026, 6, 5)


def test_terminal_close_down_ignores_window_low_touch():
    candles = [
        _candle(
            high=121.0,
            low=90.0,
            close=110.0,
            t=datetime(2026, 6, 4, tzinfo=UTC),
        ),
        _candle(
            high=125.0,
            low=95.0,
            close=120.0,
            t=datetime(2026, 6, 5, tzinfo=UTC),
        ),
    ]

    outcome, observed, _selected = classify_terminal_close_outcome(
        candles,
        review_date=date(2026, 6, 5),
        direction="down",
        target_price=100.0,
    )

    assert outcome is False
    assert observed == pytest.approx(120.0)


@pytest.mark.parametrize(("direction", "expected"), [("up", True), ("down", False)])
def test_terminal_close_equality_is_up_only(direction, expected):
    outcome, observed, _selected = classify_terminal_close_outcome(
        [
            _candle(
                high=131.0,
                low=99.0,
                close=100.0,
                t=datetime(2026, 6, 5, tzinfo=UTC),
            )
        ],
        review_date=date(2026, 6, 5),
        direction=direction,
        target_price=100.0,
    )

    assert outcome is expected
    assert observed == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("source", "expected_basis"),
    [
        ("kis", "provider_adjusted"),
        ("toss", "provider_adjusted"),
        ("toss_fallback", "provider_adjusted"),
        ("yahoo", "raw"),
        ("yahoo_fallback", "raw"),
    ],
)
def test_terminal_close_accepts_five_daily_source_contracts(
    source: str, expected_basis: str
):
    outcome, observed, selected = classify_terminal_close_outcome(
        [
            _candle(
                high=131.0,
                low=99.0,
                close=129.0,
                source=source,
                t=datetime(2026, 6, 5, tzinfo=UTC),
            )
        ],
        review_date=date(2026, 6, 5),
        direction="up",
        target_price=130.0,
    )

    assert outcome is False
    assert observed == pytest.approx(129.0)
    assert selected.source == source
    assert svc._REGULAR_SESSION_CLOSE_SOURCE_BASIS[source] == expected_basis


@pytest.mark.parametrize(
    ("candles", "expected_status"),
    [
        ([], "unresolved_no_review_candle"),
        (
            [
                _candle(
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    t=datetime(2026, 6, 4, tzinfo=UTC),
                )
            ],
            "unresolved_stale_data",
        ),
        (
            [
                _candle(
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    t=datetime(2026, 6, 5, 0, tzinfo=UTC),
                ),
                _candle(
                    high=102.0,
                    low=98.0,
                    close=101.0,
                    t=datetime(2026, 6, 5, 12, tzinfo=UTC),
                ),
            ],
            "unresolved_ambiguous_review_candle",
        ),
        (
            [
                _candle(
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    source="yahoo_extended",
                    t=datetime(2026, 6, 5, tzinfo=UTC),
                )
            ],
            "unresolved_untrusted_source",
        ),
        (
            [
                _candle(
                    high=101.0,
                    low=99.0,
                    close=float("nan"),
                    t=datetime(2026, 6, 5, tzinfo=UTC),
                )
            ],
            "unresolved_invalid_close",
        ),
    ],
)
def test_terminal_close_data_failures_are_typed(candles, expected_status):
    with pytest.raises(TerminalCloseDataError) as exc_info:
        classify_terminal_close_outcome(
            candles,
            review_date=date(2026, 6, 5),
            direction="up",
            target_price=100.0,
        )

    assert exc_info.value.status == expected_status


def test_terminal_session_gate_rejects_weekend_and_us_holiday():
    weekend = svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=date(2026, 6, 6),
        now=datetime(2026, 6, 8, 22, tzinfo=UTC),
    )
    holiday = svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=date(2026, 7, 3),
        now=datetime(2026, 7, 6, 22, tzinfo=UTC),
    )

    assert "not a XNYS regular session" in str(weekend)
    assert "not a XNYS regular session" in str(holiday)


def test_terminal_session_gate_honors_early_close_and_kr_cutoff():
    early_close_date = date(2026, 11, 27)
    assert svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=early_close_date,
        now=datetime(2026, 11, 27, 17, 59, tzinfo=UTC),
    )
    assert (
        svc._terminal_close_session_failure(
            instrument_type="equity_us",
            review_date=early_close_date,
            now=datetime(2026, 11, 27, 18, 1, tzinfo=UTC),
        )
        is None
    )

    kr_date = date(2026, 6, 5)
    assert svc._terminal_close_session_failure(
        instrument_type="equity_kr",
        review_date=kr_date,
        now=datetime(2026, 6, 5, 6, 34, tzinfo=UTC),
    )
    assert (
        svc._terminal_close_session_failure(
            instrument_type="equity_kr",
            review_date=kr_date,
            now=datetime(2026, 6, 5, 6, 35, tzinfo=UTC),
        )
        is None
    )


def test_terminal_session_gate_tracks_us_dst_close():
    before_dst_close = svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=date(2026, 3, 6),
        now=datetime(2026, 3, 6, 20, 30, tzinfo=UTC),
    )
    after_dst_close = svc._terminal_close_session_failure(
        instrument_type="equity_us",
        review_date=date(2026, 3, 9),
        now=datetime(2026, 3, 9, 20, 30, tzinfo=UTC),
    )

    assert before_dst_close is not None
    assert after_dst_close is None
