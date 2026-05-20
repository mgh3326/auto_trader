"""ROB-281 Stage 1 — KR session-aware freshness primitives.

Covers ``classify_kr_session_slot``, ``expected_kr_baseline_date``, and
``kr_session_label_for_partition``. The most important regression these tests
guard is the 07:40 – 16:19 KST window: at that time of day no same-day KRX
preliminary has run yet, so the expected baseline is the PRIOR trading day —
NOT today. Without this guard, ``/invest/screener`` would mark a fresh prior-day
partition stale right after KST midnight rollover.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.services.invest_screener_snapshots.freshness import (
    classify_kr_session_slot,
    expected_kr_baseline_date,
    kr_session_label_for_partition,
)

_KST = ZoneInfo("Asia/Seoul")


def _kst(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=_KST)


# --- classify_kr_session_slot ------------------------------------------------


@pytest.mark.parametrize(
    "hour, minute, expected_slot",
    [
        (0, 0, "nxt_final"),
        (3, 30, "nxt_final"),
        (7, 39, "nxt_final"),
        (7, 40, "pre_market_repair"),
        (10, 0, "pre_market_repair"),
        (16, 19, "pre_market_repair"),
        (16, 20, "krx_preliminary"),
        (18, 0, "krx_preliminary"),
        (20, 19, "krx_preliminary"),
        (20, 20, "nxt_final"),
        (22, 30, "nxt_final"),
        (23, 59, "nxt_final"),
    ],
)
def test_classify_kr_session_slot_boundaries(
    hour: int, minute: int, expected_slot: str
) -> None:
    # 2026-05-20 is a Wednesday, an arbitrary KR trading day.
    now = _kst(2026, 5, 20, hour, minute)
    assert classify_kr_session_slot(now) == expected_slot


def test_classify_kr_session_slot_treats_naive_input_as_utc() -> None:
    # 2026-05-20 22:40 UTC == 2026-05-21 07:40 KST → pre_market_repair.
    naive_utc = dt.datetime(2026, 5, 20, 22, 40)
    assert classify_kr_session_slot(naive_utc) == "pre_market_repair"


# --- expected_kr_baseline_date ----------------------------------------------


def test_expected_kr_baseline_pre_market_repair_window_returns_prior_day() -> None:
    """CRITICAL: 07:40 – 16:19 KST window must yield prior trading day.

    Before today's 16:20 KRX preliminary has fired, the only legitimately
    populated snapshot is the prior day's 20:20 NXT-final (or its 07:40 repair
    re-run). The expected baseline MUST be the prior trading day; otherwise a
    fresh prior-day partition would be misclassified as stale.
    """
    # Wednesday 2026-05-20 07:40 KST → expected baseline = Tuesday 2026-05-19.
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 7, 40)) == dt.date(2026, 5, 19)
    # Wednesday 2026-05-20 12:00 KST → still prior day.
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 12, 0)) == dt.date(2026, 5, 19)
    # Wednesday 2026-05-20 16:19 KST → still prior day (one minute before boundary).
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 16, 19)) == dt.date(2026, 5, 19)


def test_expected_kr_baseline_after_krx_preliminary_returns_today() -> None:
    # Wednesday 16:20 KST or later → today.
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 16, 20)) == dt.date(2026, 5, 20)
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 18, 30)) == dt.date(2026, 5, 20)
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 20, 20)) == dt.date(2026, 5, 20)
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 23, 59)) == dt.date(2026, 5, 20)


def test_expected_kr_baseline_overnight_before_repair_returns_prior_day() -> None:
    # Wednesday 03:00 KST → prior trading day = Tuesday.
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 3, 0)) == dt.date(2026, 5, 19)
    # Wednesday 07:39 KST → still prior day (one minute before repair boundary).
    assert expected_kr_baseline_date(_kst(2026, 5, 20, 7, 39)) == dt.date(2026, 5, 19)


def test_expected_kr_baseline_monday_morning_rolls_back_to_friday() -> None:
    """Mon 07:40 ~ 16:19 KST → expected baseline = Friday, not Saturday."""
    # 2026-05-18 is Monday; the prior trading weekday is Friday 2026-05-15.
    assert expected_kr_baseline_date(_kst(2026, 5, 18, 7, 40)) == dt.date(2026, 5, 15)
    assert expected_kr_baseline_date(_kst(2026, 5, 18, 12, 0)) == dt.date(2026, 5, 15)
    # Mon 00:30 KST → still prior trading day (Friday).
    assert expected_kr_baseline_date(_kst(2026, 5, 18, 0, 30)) == dt.date(2026, 5, 15)


def test_expected_kr_baseline_weekend_today_rolls_back_then_prior() -> None:
    # Saturday → "today" rolls back to Friday → before 16:20 → prior = Thursday.
    # 2026-05-23 is Saturday; "today" rolls to Fri 2026-05-22; prior weekday = Thu 2026-05-21.
    assert expected_kr_baseline_date(_kst(2026, 5, 23, 9, 0)) == dt.date(2026, 5, 21)
    # Sunday 23:00 KST → "today" rolls to Fri 2026-05-22; before 16:20 → Thu 2026-05-21.
    # (Sun 23:00 KST is after Sat 16:20, but the weekend rollback in today_kst
    # makes the time comparison happen against the rolled-back Friday's clock.
    # We still compare hm against 16:20; 23:00 >= 16:20 → today (Fri).)
    assert expected_kr_baseline_date(_kst(2026, 5, 24, 23, 0)) == dt.date(2026, 5, 22)


def test_expected_kr_baseline_naive_input_treated_as_utc() -> None:
    # 2026-05-20 22:40 UTC == 2026-05-21 07:40 KST → expected = prior weekday of Thu = Wed.
    naive_utc = dt.datetime(2026, 5, 20, 22, 40)
    # 2026-05-21 is Thursday; prior weekday = Wednesday 2026-05-20.
    assert expected_kr_baseline_date(naive_utc) == dt.date(2026, 5, 20)


# --- kr_session_label_for_partition -----------------------------------------


@pytest.mark.parametrize(
    "hour, minute, expected_label",
    [
        (16, 20, "KRX preliminary"),
        (17, 0, "KRX preliminary"),
        (20, 19, "KRX preliminary"),
        (20, 20, "NXT final"),
        (22, 30, "NXT final"),
        (23, 59, "NXT final"),
        (0, 30, "NXT final"),  # overnight tail
        (6, 59, "NXT final"),  # overnight tail upper edge
        (7, 40, "NXT final"),  # 07:40 repair window targets prior NXT-final
        (10, 0, "NXT final"),  # repair window middle
        (16, 19, "NXT final"),  # repair window upper edge
    ],
)
def test_kr_session_label_for_partition_known_windows(
    hour: int, minute: int, expected_label: str
) -> None:
    computed_at = _kst(2026, 5, 20, hour, minute)
    assert kr_session_label_for_partition(computed_at) == expected_label


@pytest.mark.parametrize("hour, minute", [(7, 0), (7, 20), (7, 39)])
def test_kr_session_label_for_partition_gap_returns_none(
    hour: int, minute: int
) -> None:
    """The 07:00 – 07:39 KST gap has no scheduled slot — return None."""
    computed_at = _kst(2026, 5, 20, hour, minute)
    assert kr_session_label_for_partition(computed_at) is None


def test_kr_session_label_for_partition_none_input_returns_none() -> None:
    assert kr_session_label_for_partition(None) is None


def test_kr_session_label_for_partition_naive_input_treated_as_utc() -> None:
    # 2026-05-20 07:20 UTC == 2026-05-20 16:20 KST → "KRX preliminary".
    naive_utc = dt.datetime(2026, 5, 20, 7, 20)
    assert kr_session_label_for_partition(naive_utc) == "KRX preliminary"
