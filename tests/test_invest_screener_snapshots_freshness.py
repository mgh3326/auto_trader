import datetime as dt

import pytest

from app.services.invest_screener_snapshots.freshness import (
    aggregate_states,
    classify_state,
    today_trading_date,
)


def test_today_trading_date_kr_weekend_rolls_back():
    sat = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)  # Sat
    assert today_trading_date("kr", now=sat) == dt.date(2026, 5, 8)  # Fri


def test_classify_state_fresh_when_window_long_enough():
    snap_date = dt.date(2026, 5, 9)
    now = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)
    state = classify_state(
        snapshot_date=snap_date,
        computed_at=dt.datetime(2026, 5, 9, 5, 0, tzinfo=dt.UTC),
        closes_window_len=10,
        today_trading_date_value=snap_date,
        now=now,
    )
    assert state == "fresh"


def test_classify_state_partial_short_window():
    snap_date = dt.date(2026, 5, 9)
    now = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)
    state = classify_state(
        snapshot_date=snap_date,
        computed_at=now,
        closes_window_len=3,
        today_trading_date_value=snap_date,
        now=now,
    )
    assert state == "partial"


def test_classify_state_stale_when_old_or_old_computed():
    today = dt.date(2026, 5, 9)
    now = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)
    # Old snapshot_date.
    assert (
        classify_state(
            snapshot_date=dt.date(2026, 5, 1),
            computed_at=now,
            closes_window_len=10,
            today_trading_date_value=today,
            now=now,
        )
        == "stale"
    )
    # Old computed_at (>= 36h).
    assert (
        classify_state(
            snapshot_date=today,
            computed_at=dt.datetime(2026, 5, 7, 10, 0, tzinfo=dt.UTC),
            closes_window_len=10,
            today_trading_date_value=today,
            now=now,
        )
        == "stale"
    )


@pytest.mark.parametrize(
    "states,expected",
    [
        (["fresh", "fresh", "fresh"], "fresh"),
        (["fresh", "partial"], "partial"),
        (["fresh", "stale"], "stale"),
        (["fresh", "missing"], "fallback"),
        ([], "missing"),
    ],
)
def test_aggregate_states(states, expected):
    assert aggregate_states(states) == expected
