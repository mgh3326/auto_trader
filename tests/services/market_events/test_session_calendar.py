"""Unit tests for the fail-closed XNYS/XKRX session calendar (ROB-371)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.session_calendar import (
    is_trading_session,
    next_trading_session,
    previous_trading_session,
    trading_sessions_in_range,
)


@pytest.mark.unit
def test_weekend_is_not_a_session():
    assert is_trading_session("us", date(2026, 5, 9)) is False  # Saturday
    assert is_trading_session("us", date(2026, 5, 10)) is False  # Sunday


@pytest.mark.unit
def test_us_holiday_is_not_a_session():
    # 2025-07-04 Independence Day — XNYS closed.
    assert is_trading_session("us", date(2025, 7, 4)) is False
    # 2025-12-25 Christmas — XNYS closed.
    assert is_trading_session("us", date(2025, 12, 25)) is False


@pytest.mark.unit
def test_us_regular_weekday_is_a_session():
    assert is_trading_session("us", date(2025, 7, 7)) is True  # Monday, open


@pytest.mark.unit
def test_kr_holiday_is_not_a_session():
    # 2025-01-01 New Year — XKRX closed.
    assert is_trading_session("kr", date(2025, 1, 1)) is False


@pytest.mark.unit
def test_far_future_date_fails_closed():
    # Beyond the calendar's precomputed range -> not a session, never an error.
    assert is_trading_session("us", date(2100, 1, 4)) is False


@pytest.mark.unit
def test_far_past_date_fails_closed():
    # Before the calendar's range -> not a session, never an error.
    assert is_trading_session("us", date(1900, 1, 2)) is False


@pytest.mark.unit
def test_next_and_previous_skip_holiday_and_weekend():
    # Thu 2025-07-03 session; Fri 2025-07-04 holiday; next session Mon 2025-07-07.
    assert next_trading_session("us", date(2025, 7, 3)) == date(2025, 7, 7)
    assert previous_trading_session("us", date(2025, 7, 7)) == date(2025, 7, 3)


@pytest.mark.unit
def test_next_trading_session_unresolvable_returns_none():
    assert next_trading_session("us", date(2100, 1, 1)) is None
    assert previous_trading_session("us", date(1900, 1, 1)) is None


@pytest.mark.unit
def test_trading_sessions_in_range_excludes_weekends_holidays():
    sessions = trading_sessions_in_range("us", date(2025, 7, 1), date(2025, 7, 8))
    # Jul 1,2,3 (Tue-Thu sessions), 4 holiday, 5-6 weekend, 7,8 (Mon-Tue sessions).
    assert date(2025, 7, 4) not in sessions
    assert date(2025, 7, 5) not in sessions
    assert date(2025, 7, 3) in sessions
    assert date(2025, 7, 7) in sessions


@pytest.mark.unit
def test_trading_sessions_in_range_out_of_bounds_is_empty():
    assert trading_sessions_in_range("us", date(2100, 1, 1), date(2100, 1, 10)) == []


@pytest.mark.unit
def test_trading_sessions_in_range_reversed_is_empty():
    assert trading_sessions_in_range("us", date(2025, 7, 8), date(2025, 7, 1)) == []
