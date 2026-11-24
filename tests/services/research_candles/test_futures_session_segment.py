"""hk 6148: futures session segment and trading date (pure, no DB, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.research_candles import session_segment as ss
from app.services.research_candles.session_segment import (
    futures_session_date,
    session_segment_of,
)

pytestmark = pytest.mark.unit

KST = ZoneInfo("Asia/Seoul")


def _kst(day: tuple[int, int, int], hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(*day, hour, minute, tzinfo=KST)


TUE = (2026, 11, 24)  # XKRX session
WED = (2026, 11, 25)
FRI = (2026, 11, 27)
SAT = (2026, 11, 28)
SUN = (2026, 11, 29)
MON = (2026, 11, 30)
EVE_OF_HOLIDAY = (2026, 12, 24)  # Thursday; 2026-12-25 (Fri) is closed
HOLIDAY = (2026, 12, 25)

BOUNDARIES = [
    (TUE, "08:44", "UNKNOWN"),
    (TUE, "08:45", "FUT_DAY"),
    (TUE, "15:44", "FUT_DAY"),
    (TUE, "15:45", "UNKNOWN"),
    (TUE, "17:59", "UNKNOWN"),
    (TUE, "18:00", "FUT_NIGHT"),
    (WED, "05:59", "FUT_NIGHT"),
    (WED, "06:00", "UNKNOWN"),
]


@pytest.mark.parametrize(("day", "hhmm", "expected"), BOUNDARIES)
def test_boundaries_are_start_inclusive_end_exclusive(day, hhmm, expected) -> None:
    assert session_segment_of("futures", "KRX", _kst(day, hhmm)) == expected


def test_day_bar_session_date_is_its_kst_date() -> None:
    assert futures_session_date(_kst(TUE, "09:00")) == date(*TUE)


def test_night_bar_carries_the_next_trading_day_across_midnight() -> None:
    assert futures_session_date(_kst(TUE, "18:00")) == date(*WED)
    assert futures_session_date(_kst(TUE, "23:59")) == date(*WED)
    # after midnight the bar belongs to the night that started the day before
    assert futures_session_date(_kst(WED, "00:00")) == date(*WED)
    assert futures_session_date(_kst(WED, "05:59")) == date(*WED)


def test_friday_night_belongs_to_monday() -> None:
    assert futures_session_date(_kst(FRI, "19:00")) == date(*MON)
    assert futures_session_date(_kst(SAT, "02:00")) == date(*MON)  # still Friday's night
    assert session_segment_of("futures", "KRX", _kst(SAT, "02:00")) == "FUT_NIGHT"


def test_holiday_eve_night_belongs_to_the_next_trading_day() -> None:
    # 12-24 night -> 12-25 is closed -> next trading day 12-28 (Monday)
    assert futures_session_date(_kst(EVE_OF_HOLIDAY, "20:00")) == date(2026, 12, 28)
    assert futures_session_date(_kst(HOLIDAY, "03:00")) == date(2026, 12, 28)


def test_night_that_starts_on_a_non_trading_day_is_unknown() -> None:
    for moment in (_kst(HOLIDAY, "19:00"), _kst(SAT, "19:00"), _kst(SUN, "03:00")):
        assert session_segment_of("futures", "KRX", moment) == "UNKNOWN"
        assert futures_session_date(moment) is None


def test_day_window_on_a_holiday_is_unknown_with_null_date() -> None:
    moment = _kst(HOLIDAY, "10:00")
    assert session_segment_of("futures", "KRX", moment) == "UNKNOWN"
    assert futures_session_date(moment) is None


def test_unknown_calendar_gives_unknown_and_null(monkeypatch) -> None:
    monkeypatch.setattr(ss, "_kr_session_day", lambda day: False)
    ss._next_kr_trading_day.cache_clear()
    for moment in (_kst(TUE, "10:00"), _kst(TUE, "19:00")):
        assert session_segment_of("futures", "KRX", moment) == "UNKNOWN"
        assert futures_session_date(moment) is None
    ss._next_kr_trading_day.cache_clear()


def test_naive_timestamps_and_other_markets_are_unchanged() -> None:
    naive = datetime(2026, 11, 24, 10, 0)
    assert session_segment_of("futures", "KRX", naive) == "UNKNOWN"
    assert futures_session_date(naive) is None
    assert session_segment_of("crypto", "KRX", _kst(TUE, "10:00")) == "UNKNOWN"
    # KR is untouched by the futures branch
    assert session_segment_of("kr", "KRX", _kst(TUE, "09:00")) == "KRX_REGULAR"


def test_utc_input_is_converted() -> None:
    assert (
        session_segment_of("futures", "KRX", datetime(2026, 11, 23, 23, 45, tzinfo=UTC))
        == "FUT_DAY"
    )  # 08:45 KST on the 24th
