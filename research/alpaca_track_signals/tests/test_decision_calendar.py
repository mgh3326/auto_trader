from __future__ import annotations

from datetime import UTC, datetime

import pytest

import decision_calendar as dc


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


def test_ap_a1_decision_ts_is_exactly_00_05_00_utc_any_day():
    assert dc.is_ap_a1_decision_ts(_ms(2026, 7, 20, 0, 5, 0))
    assert not dc.is_ap_a1_decision_ts(_ms(2026, 7, 20, 0, 4, 59))
    assert not dc.is_ap_a1_decision_ts(_ms(2026, 7, 20, 0, 5, 1))
    assert not dc.is_ap_a1_decision_ts(_ms(2026, 7, 20, 0, 6, 0))


def test_ap_a1_decision_ts_rejects_non_zero_milliseconds():
    ts = _ms(2026, 7, 20, 0, 5, 0) + 1
    assert not dc.is_ap_a1_decision_ts(ts)


def test_ap_a2_decision_ts_requires_monday():
    # 2026-07-20 is a Monday.
    monday = _ms(2026, 7, 20, 0, 5, 0)
    tuesday = _ms(2026, 7, 21, 0, 5, 0)
    assert dc.is_ap_a2_decision_ts(monday)
    assert not dc.is_ap_a2_decision_ts(tuesday)


def test_ap_a2_decision_ts_still_requires_the_00_05_time_on_monday():
    monday_wrong_time = _ms(2026, 7, 20, 12, 0, 0)
    assert not dc.is_ap_a2_decision_ts(monday_wrong_time)


def test_prior_completed_day_window_is_the_day_before_the_decision_day():
    decision = _ms(2026, 7, 20, 0, 5, 0)
    start, end = dc.prior_completed_day_window(decision)
    assert end == _ms(2026, 7, 20, 0, 0, 0)
    assert start == _ms(2026, 7, 19, 0, 0, 0)
    assert end - start == dc.DAY_MS


def test_prior_completed_day_window_never_includes_the_decision_days_own_data():
    """The in-progress decision day's own [00:00, decision_ts) partial window
    must never be reachable from this function's output — end_ms is that
    day's 00:00, strictly before ANY of that day's own minutes."""
    decision = _ms(2026, 7, 20, 0, 5, 0)
    _start, end = dc.prior_completed_day_window(decision)
    decision_day_start = _ms(2026, 7, 20, 0, 0, 0)
    assert end == decision_day_start
    assert end <= decision  # never reaches into the decision day at all


def test_prior_completed_day_window_rejects_a_non_decision_timestamp():
    with pytest.raises(ValueError, match="decision timestamp"):
        dc.prior_completed_day_window(_ms(2026, 7, 20, 12, 0, 0))


def test_int_type_discipline_rejects_bool_and_non_int():
    with pytest.raises(TypeError):
        dc.is_ap_a1_decision_ts(True)  # bool is a strict int subclass
    with pytest.raises(TypeError):
        dc.is_ap_a1_decision_ts(1.0)
