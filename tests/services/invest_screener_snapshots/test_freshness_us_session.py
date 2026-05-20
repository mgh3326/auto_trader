"""ROB-281 Stage 2 — US session-aware freshness primitives.

Covers ``expected_us_baseline_date``, ``last_completed_us_session_close``, and
``us_session_label_for_partition``. Validates that the XNYS calendar via
``exchange_calendars`` correctly distinguishes regular trading days, NYSE
holidays, half-days (e.g., Black Friday 13:00 ET close), and DST boundary
behavior. The 17:20 ET cutoff is the chosen US post-close slot per ROB-281 D2.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.services.invest_screener_snapshots.freshness import (
    expected_us_baseline_date,
    last_completed_us_session_close,
    us_session_label_for_partition,
)

_ET = ZoneInfo("America/New_York")
_UTC = dt.UTC


def _et(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=_ET)


# --- expected_us_baseline_date ----------------------------------------------


def test_expected_us_baseline_regular_day_post_close_returns_today() -> None:
    # 2025-06-09 is a regular Monday; 17:20 ET is post-close.
    assert expected_us_baseline_date(_et(2025, 6, 9, 17, 20)) == dt.date(2025, 6, 9)
    # 18:00 ET same day — still today.
    assert expected_us_baseline_date(_et(2025, 6, 9, 18, 0)) == dt.date(2025, 6, 9)


def test_expected_us_baseline_regular_day_before_post_close_returns_prior() -> None:
    # 17:19 ET on a session day — one minute before the post-close threshold.
    # Prior session for Mon 2025-06-09 is Fri 2025-06-06.
    assert expected_us_baseline_date(_et(2025, 6, 9, 17, 19)) == dt.date(2025, 6, 6)
    # Morning of the same session — still prior.
    assert expected_us_baseline_date(_et(2025, 6, 9, 9, 30)) == dt.date(2025, 6, 6)


def test_expected_us_baseline_weekend_returns_prior_friday() -> None:
    # Sat 2025-06-14 → prior session = Fri 2025-06-13.
    assert expected_us_baseline_date(_et(2025, 6, 14, 12, 0)) == dt.date(2025, 6, 13)
    # Sun 2025-06-15 → still Fri 2025-06-13.
    assert expected_us_baseline_date(_et(2025, 6, 15, 23, 0)) == dt.date(2025, 6, 13)


def test_expected_us_baseline_holiday_skips_to_prior_session() -> None:
    """2025-07-04 is Independence Day (Friday) — NYSE closed.

    Prior trading session is Thu 2025-07-03 (regular session).
    """
    assert expected_us_baseline_date(_et(2025, 7, 4, 18, 0)) == dt.date(2025, 7, 3)
    # Morning of the same holiday → still Thu.
    assert expected_us_baseline_date(_et(2025, 7, 4, 9, 0)) == dt.date(2025, 7, 3)


def test_expected_us_baseline_thanksgiving_weekend() -> None:
    """Thanksgiving 2025: Thu Nov 27 (closed), Fri Nov 28 (half-day).

    Half-day Friday is still a SESSION — 17:20 ET on Nov 28 → today (Nov 28),
    not Wed Nov 26. This validates that half-day handling does not require
    special-casing.
    """
    # Thu 2025-11-27 (holiday) at 18:00 ET → prior = Wed 2025-11-26.
    assert expected_us_baseline_date(_et(2025, 11, 27, 18, 0)) == dt.date(2025, 11, 26)
    # Fri 2025-11-28 (half-day) at 17:20 ET → today (Fri Nov 28).
    assert expected_us_baseline_date(_et(2025, 11, 28, 17, 20)) == dt.date(2025, 11, 28)
    # Fri 2025-11-28 (half-day) at 14:00 ET → before 17:20 cutoff → prior = Wed Nov 26.
    assert expected_us_baseline_date(_et(2025, 11, 28, 14, 0)) == dt.date(2025, 11, 26)


def test_expected_us_baseline_dst_spring_forward_boundary() -> None:
    """DST starts second Sunday of March (2026-03-08).

    Sun itself is a non-session day. The first post-DST trading day is
    Mon 2026-03-09; 17:20 ET on that Monday → today (Mon).
    """
    # Sun 2026-03-08 (DST transition day) at noon ET → prior Fri 2026-03-06.
    assert expected_us_baseline_date(_et(2026, 3, 8, 12, 0)) == dt.date(2026, 3, 6)
    # Mon 2026-03-09 at 17:20 ET (post-DST) → today.
    assert expected_us_baseline_date(_et(2026, 3, 9, 17, 20)) == dt.date(2026, 3, 9)


def test_expected_us_baseline_naive_input_treated_as_utc() -> None:
    # 2025-06-09 21:20 UTC == 2025-06-09 17:20 EDT (DST active in June).
    naive_utc = dt.datetime(2025, 6, 9, 21, 20)
    assert expected_us_baseline_date(naive_utc) == dt.date(2025, 6, 9)
    # 2025-06-09 21:19 UTC == 2025-06-09 17:19 EDT — one minute before cutoff.
    naive_utc_before = dt.datetime(2025, 6, 9, 21, 19)
    assert expected_us_baseline_date(naive_utc_before) == dt.date(2025, 6, 6)


# --- last_completed_us_session_close ----------------------------------------


def test_last_completed_us_session_close_after_regular_close() -> None:
    """Regular NYSE close is 16:00 ET. At 17:20 ET Monday, last close = today 16:00 ET."""
    # 17:20 EDT == 21:20 UTC; expected close = 20:00 UTC (16:00 EDT).
    close = last_completed_us_session_close(_et(2025, 6, 9, 17, 20))
    assert close is not None
    assert close.astimezone(_ET).date() == dt.date(2025, 6, 9)
    assert close.astimezone(_ET).hour == 16  # regular close


def test_last_completed_us_session_close_before_today_close_returns_prior() -> None:
    """At 12:00 ET Monday, today's close hasn't happened yet → prior Friday's close."""
    close = last_completed_us_session_close(_et(2025, 6, 9, 12, 0))
    assert close is not None
    assert close.astimezone(_ET).date() == dt.date(2025, 6, 6)


def test_last_completed_us_session_close_half_day_returns_early_close() -> None:
    """Black Friday 2025-11-28 closes at 13:00 ET (half-day).

    At 14:00 ET (after early close), last close = 13:00 ET today.
    """
    close = last_completed_us_session_close(_et(2025, 11, 28, 14, 0))
    assert close is not None
    close_et = close.astimezone(_ET)
    assert close_et.date() == dt.date(2025, 11, 28)
    assert close_et.hour == 13  # half-day early close


# --- us_session_label_for_partition -----------------------------------------


def test_us_session_label_for_partition_returns_post_close_for_any_datetime() -> None:
    assert (
        us_session_label_for_partition(_et(2025, 6, 9, 17, 20)) == "US post-close"
    )
    assert (
        us_session_label_for_partition(_et(2025, 11, 28, 13, 5)) == "US post-close"
    )
    # Even at unusual hours, the label is constant — the partition's existence
    # is what matters, not which time-of-day it was computed.
    assert (
        us_session_label_for_partition(_et(2025, 6, 9, 9, 30)) == "US post-close"
    )


def test_us_session_label_for_partition_none_input_returns_none() -> None:
    assert us_session_label_for_partition(None) is None


def test_us_session_label_for_partition_accepts_naive_utc() -> None:
    naive_utc = dt.datetime(2025, 6, 9, 21, 20)
    assert us_session_label_for_partition(naive_utc) == "US post-close"
