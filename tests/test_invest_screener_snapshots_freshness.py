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


from zoneinfo import ZoneInfo  # noqa: E402

from app.services.invest_screener_snapshots.freshness import (  # noqa: E402
    classify_investor_flow_partition,
    compute_overall_state,
    format_kst_as_of_label,
)

_KST = ZoneInfo("Asia/Seoul")


def test_classify_investor_flow_partition_fresh_when_partition_is_today_trading_date() -> (
    None
):
    today = dt.date(2026, 5, 20)  # Wednesday
    state = classify_investor_flow_partition(
        snapshot_date=today,
        collected_at=dt.datetime(2026, 5, 20, 7, 30, tzinfo=dt.UTC),
        today_trading_date_value=today,
        now=dt.datetime(2026, 5, 20, 8, 0, tzinfo=dt.UTC),
    )
    assert state == "fresh"


def test_classify_investor_flow_partition_stale_when_partition_is_two_trading_days_old() -> (
    None
):
    state = classify_investor_flow_partition(
        snapshot_date=dt.date(2026, 5, 18),  # Monday
        collected_at=dt.datetime(2026, 5, 18, 7, 30, tzinfo=dt.UTC),
        today_trading_date_value=dt.date(2026, 5, 20),  # Wednesday
        now=dt.datetime(2026, 5, 20, 8, 0, tzinfo=dt.UTC),
    )
    assert state == "stale"


def test_classify_investor_flow_partition_friday_snapshot_on_saturday_noon_is_fresh() -> (
    None
):
    """Weekend rollback: on Saturday, today_trading_date rolls back to Friday, so
    a Friday partition with collected_at within STALE_AFTER_HOURS is fresh."""
    state = classify_investor_flow_partition(
        snapshot_date=dt.date(2026, 5, 15),  # Friday
        collected_at=dt.datetime(2026, 5, 15, 7, 30, tzinfo=dt.UTC),
        today_trading_date_value=dt.date(
            2026, 5, 15
        ),  # what today_trading_date("kr") returns on Sat
        now=dt.datetime(2026, 5, 16, 3, 0, tzinfo=dt.UTC),  # Sat 12:00 KST
    )
    assert state == "fresh"


def test_compute_overall_state_primary_stale_dominates() -> None:
    assert (
        compute_overall_state(primary_state="stale", dependency_states=["fresh"])
        == "stale"
    )


def test_compute_overall_state_primary_missing_dominates() -> None:
    assert (
        compute_overall_state(primary_state="missing", dependency_states=["fresh"])
        == "missing"
    )


def test_compute_overall_state_primary_fresh_dependency_stale_is_stale() -> None:
    assert (
        compute_overall_state(primary_state="fresh", dependency_states=["stale"])
        == "stale"
    )


def test_compute_overall_state_primary_fresh_dependency_missing_is_stale() -> None:
    assert (
        compute_overall_state(primary_state="fresh", dependency_states=["missing"])
        == "stale"
    )


def test_compute_overall_state_primary_fresh_dependency_partial_is_partial() -> None:
    assert (
        compute_overall_state(primary_state="fresh", dependency_states=["partial"])
        == "partial"
    )


def test_compute_overall_state_primary_fresh_no_dependencies_is_fresh() -> None:
    assert compute_overall_state(primary_state="fresh", dependency_states=[]) == "fresh"


def test_format_kst_as_of_label_for_snapshot_date_only_uses_jangmagam() -> None:
    label = format_kst_as_of_label(snapshot_date=dt.date(2026, 5, 13), computed_at=None)
    assert label == "2026.05.13 장마감 기준"


def test_format_kst_as_of_label_with_computed_at_uses_hhmm() -> None:
    label = format_kst_as_of_label(
        snapshot_date=dt.date(2026, 5, 20),
        computed_at=dt.datetime(2026, 5, 20, 0, 35, tzinfo=dt.UTC),  # 09:35 KST
    )
    assert label == "2026.05.20 09:35 기준"


def test_classify_momentum_freshness_fresh_when_latest_matches_baseline():
    from app.services.invest_screener_snapshots.freshness import (
        classify_momentum_freshness,
        expected_kr_baseline_date,
    )

    now = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)  # 18:00 KST Mon
    expected = expected_kr_baseline_date(now)
    state, days_stale = classify_momentum_freshness(
        latest_trading_date=expected, now=now
    )
    assert state == "fresh"
    assert days_stale == 0


def test_classify_momentum_freshness_stale_with_days_elapsed():
    from app.services.invest_screener_snapshots.freshness import (
        classify_momentum_freshness,
        expected_kr_baseline_date,
    )

    now = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    expected = expected_kr_baseline_date(now)
    old = expected - dt.timedelta(days=14)
    state, days_stale = classify_momentum_freshness(latest_trading_date=old, now=now)
    assert state == "stale"
    assert days_stale == 14
