from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from zoneinfo import ZoneInfo

import pytest

from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events import freshness_service as fs
from app.services.market_events.freshness_service import (
    MarketEventsFreshnessService,
    _is_stale,
    _Key,
)

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
THRESHOLD_HOURS = 30.0

# 2026-01-05 is a Monday; 2026-01-09 is a Friday; 2026-01-12 is a Monday.
FRI_EVENING_KST = dt.datetime(2026, 1, 9, 18, 30, tzinfo=KST)
MON_MORNING_KST = dt.datetime(2026, 1, 12, 8, 0, tzinfo=KST)


def _weekend_closed(market: str, day: dt.date) -> str:
    return "closed" if day.weekday() >= 5 else "open"


def _all_open(market: str, day: dt.date) -> str:
    return "open"


def _all_unknown(market: str, day: dt.date) -> str:
    return "unknown"


def _patch_calendar(
    monkeypatch: pytest.MonkeyPatch,
    status_for: Callable[[str, dt.date], str],
) -> None:
    monkeypatch.setattr(fs, "trading_session_status", status_for)


def _succeeded(
    finished_at: dt.datetime, *, market: str = "kr"
) -> MarketEventIngestionPartition:
    return MarketEventIngestionPartition(
        source="dart",
        category="disclosure",
        market=market,
        partition_date=finished_at.date(),
        status="succeeded",
        finished_at=finished_at,
        last_error=None,
    )


def _row_stale(
    partitions: list[MarketEventIngestionPartition],
    *,
    market: str,
    now: dt.datetime,
) -> bool:
    service = MarketEventsFreshnessService(
        None
    )  # the row builder never uses the session
    row = service._row_for_key(
        key=_Key(source="dart", category="disclosure", market=market),
        partitions=partitions,
        event_count=0,
        window_from=dt.date(2026, 1, 1),
        window_to=dt.date(2026, 1, 31),
        stale_threshold_hours=THRESHOLD_HOURS,
        expected_next_refresh=None,
        now=now,
    )
    return row.stale


@pytest.mark.unit
@pytest.mark.parametrize(
    ("market", "finished", "now"),
    [
        (
            "kr",
            FRI_EVENING_KST,
            MON_MORNING_KST,
        ),
        (
            "us",
            dt.datetime(2026, 1, 9, 16, 0, tzinfo=NY),
            dt.datetime(2026, 1, 12, 9, 0, tzinfo=NY),
        ),
    ],
)
def test_weekend_closed_days_extend_threshold_for_kr_and_us(
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    finished: dt.datetime,
    now: dt.datetime,
) -> None:
    _patch_calendar(monkeypatch, _weekend_closed)

    # Sanity: the plain 30 h rule alone would call this stale.
    assert (now - finished).total_seconds() / 3600 > THRESHOLD_HOURS
    assert fs._non_session_days(market, finished, now) == 2

    assert (
        _row_stale([_succeeded(finished, market=market)], market=market, now=now)
        is False
    )
    assert _is_stale(finished, now=now, market=market) is False


@pytest.mark.unit
def test_open_weekday_gap_stays_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_calendar(monkeypatch, _all_open)
    finished = dt.datetime(2026, 1, 5, 18, 30, tzinfo=KST)  # Mon
    now = dt.datetime(2026, 1, 7, 8, 0, tzinfo=KST)  # Wed, 37.5 h later

    assert fs._non_session_days("kr", finished, now) == 0
    assert _row_stale([_succeeded(finished)], market="kr", now=now) is True
    assert _is_stale(finished, now=now, market="kr") is True


@pytest.mark.unit
def test_closed_weekday_holiday_extends_threshold_by_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holiday = dt.date(2026, 1, 6)  # Tue, marked closed by the calendar

    def status_for(market: str, day: dt.date) -> str:
        return "closed" if day == holiday else "open"

    _patch_calendar(monkeypatch, status_for)
    finished = dt.datetime(2026, 1, 5, 18, 30, tzinfo=KST)  # Mon
    now = dt.datetime(2026, 1, 7, 8, 0, tzinfo=KST)  # Wed, 37.5 h later

    assert fs._non_session_days("kr", finished, now) == 1
    assert _row_stale([_succeeded(finished)], market="kr", now=now) is False
    assert _is_stale(finished, now=now, market="kr") is False


@pytest.mark.unit
def test_unknown_calendar_never_extends_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_calendar(monkeypatch, _all_unknown)
    finished = FRI_EVENING_KST
    now = MON_MORNING_KST  # 61.5 h later, same geometry as the weekend case

    assert (now - finished).total_seconds() / 3600 > THRESHOLD_HOURS
    assert fs._non_session_days("kr", finished, now) == 0
    assert _row_stale([_succeeded(finished)], market="kr", now=now) is True
    assert _is_stale(finished, now=now, market="kr") is True


@pytest.mark.unit
def test_market_without_calendar_uses_plain_hour_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_calendar(monkeypatch, _weekend_closed)
    finished = FRI_EVENING_KST
    now = MON_MORNING_KST

    assert fs._non_session_days("crypto", finished, now) == 0
    assert (
        _row_stale([_succeeded(finished, market="crypto")], market="crypto", now=now)
        is True
    )
    assert _is_stale(finished, now=now, market="crypto") is True
    assert _is_stale(finished, now=now) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "partitions",
    [
        [],
        [
            MarketEventIngestionPartition(
                source="dart",
                category="disclosure",
                market="kr",
                partition_date=dt.date(2026, 1, 9),
                status="failed",
                finished_at=None,
                last_error="boom",
            )
        ],
    ],
    ids=["no-partitions", "only-failed"],
)
def test_no_successful_partition_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    partitions: list[MarketEventIngestionPartition],
) -> None:
    _patch_calendar(monkeypatch, _all_open)

    assert _row_stale(partitions, market="kr", now=MON_MORNING_KST) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("now", "expected_days"),
    [
        # Sat 00:01: Fri was a trading day and its run is missing; Sat is today
        # and must not add 24 h just after midnight.
        (dt.datetime(2026, 1, 10, 0, 1, tzinfo=KST), 0),
        # Sun 10:00: only Sat (closed, before today) counts; 64 h > 54 h.
        (dt.datetime(2026, 1, 11, 10, 0, tzinfo=KST), 1),
    ],
)
def test_today_is_not_counted_so_a_missed_trading_day_stays_stale(
    monkeypatch: pytest.MonkeyPatch, now: dt.datetime, expected_days: int
) -> None:
    _patch_calendar(monkeypatch, _weekend_closed)
    finished = dt.datetime(2026, 1, 8, 18, 0, tzinfo=KST)  # Thu

    assert fs._non_session_days("kr", finished, now) == expected_days
    assert _row_stale([_succeeded(finished)], market="kr", now=now) is True
    # The 36 h per-day rule uses the same allowance: stale exactly when the age
    # exceeds 36 h plus 24 h per counted closed day.
    age_hours = (now - finished).total_seconds() / 3600
    assert _is_stale(finished, now=now, market="kr") is (
        age_hours > fs.STALE_AFTER_HOURS + 24 * expected_days
    )
