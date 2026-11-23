"""hk 6145: ``session_segment_of`` boundaries (pure, no DB, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.research_candles.session_segment import session_segment_of

pytestmark = pytest.mark.unit

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

KR_DAY = (2026, 11, 24)  # Tuesday, XKRX session
KR_HOLIDAY = (2026, 12, 25)  # Friday, 성탄절 (closed)
US_EDT_DAY = (2026, 7, 15)  # Wednesday, DST in force
US_EST_DAY = (2026, 12, 15)  # Tuesday, standard time
US_EARLY_CLOSE = (2026, 11, 27)  # day after Thanksgiving, closes 13:00 ET
US_HOLIDAY = (2026, 11, 26)  # Thanksgiving


def _kst(day: tuple[int, int, int], hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(*day, hour, minute, tzinfo=KST)


def _et(day: tuple[int, int, int], hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(*day, hour, minute, tzinfo=ET)


KR_CASES = [
    # time, KRX, NTX
    ("07:59", "UNKNOWN", "UNKNOWN"),
    ("08:00", "UNKNOWN", "NXT_PRE"),
    ("08:49", "UNKNOWN", "NXT_PRE"),
    ("08:50", "UNKNOWN", "UNKNOWN"),
    ("08:59", "UNKNOWN", "UNKNOWN"),
    ("09:00", "KRX_REGULAR", "NXT_OVERLAP"),
    ("15:29", "KRX_REGULAR", "NXT_OVERLAP"),
    ("15:30", "UNKNOWN", "NXT_POST"),
    ("15:59", "UNKNOWN", "NXT_POST"),
    ("16:00", "KRX_AFTER", "NXT_POST"),
    ("19:59", "KRX_AFTER", "NXT_POST"),
    ("20:00", "UNKNOWN", "UNKNOWN"),
]


@pytest.mark.parametrize(("clock", "krx", "ntx"), KR_CASES)
def test_kr_segments_by_venue(clock: str, krx: str, ntx: str) -> None:
    bar = _kst(KR_DAY, clock)
    assert session_segment_of("kr", "KRX", bar) == krx
    assert session_segment_of("kr", "NTX", bar) == ntx


def test_kr_holiday_is_unknown_for_every_clock_time() -> None:
    for clock, _, _ in KR_CASES:
        bar = _kst(KR_HOLIDAY, clock)
        assert session_segment_of("kr", "KRX", bar) == "UNKNOWN"
        assert session_segment_of("kr", "NTX", bar) == "UNKNOWN"


def test_kr_weekend_is_unknown() -> None:
    assert session_segment_of("kr", "KRX", _kst((2026, 11, 21), "10:00")) == "UNKNOWN"


def test_kr_unknown_venue_is_unknown() -> None:
    assert session_segment_of("kr", "XXX", _kst(KR_DAY, "10:00")) == "UNKNOWN"


US_CASES = [
    ("03:59", "UNKNOWN"),
    ("04:00", "US_PRE"),
    ("09:29", "US_PRE"),
    ("09:30", "US_REGULAR"),
    ("15:59", "US_REGULAR"),
    ("16:00", "US_POST"),
    ("19:59", "US_POST"),
    ("20:00", "UNKNOWN"),
]


@pytest.mark.parametrize("day", [US_EDT_DAY, US_EST_DAY], ids=["edt", "est"])
@pytest.mark.parametrize(("clock", "expected"), US_CASES)
def test_us_segments_on_edt_and_est_days(
    day: tuple[int, int, int], clock: str, expected: str
) -> None:
    for venue in ("NASD", "NYSE", "AMEX"):
        assert session_segment_of("us", venue, _et(day, clock)) == expected


def test_us_edt_and_est_open_are_different_utc_instants() -> None:
    # 09:30 ET is 13:30 UTC in summer and 14:30 UTC in winter; the label follows ET.
    summer = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    winter = datetime(2026, 12, 15, 14, 30, tzinfo=UTC)
    assert session_segment_of("us", "NASD", summer) == "US_REGULAR"
    assert session_segment_of("us", "NASD", winter) == "US_REGULAR"
    assert session_segment_of("us", "NASD", summer.replace(minute=29)) == "US_PRE"
    assert session_segment_of("us", "NASD", winter.replace(minute=29)) == "US_PRE"


def test_us_early_close_moves_post_market_start() -> None:
    assert session_segment_of("us", "NASD", _et(US_EARLY_CLOSE, "12:59")) == "US_REGULAR"
    assert session_segment_of("us", "NASD", _et(US_EARLY_CLOSE, "13:00")) == "US_POST"


def test_us_holiday_is_unknown() -> None:
    assert session_segment_of("us", "NASD", _et(US_HOLIDAY, "10:00")) == "UNKNOWN"


def test_unknown_market_venue_or_naive_timestamp_is_unknown() -> None:
    assert session_segment_of("crypto", "NASD", _et(US_EDT_DAY, "10:00")) == "UNKNOWN"
    assert session_segment_of("us", "KRX", _et(US_EDT_DAY, "10:00")) == "UNKNOWN"
    assert session_segment_of("us", "NASD", datetime(2026, 7, 15, 14, 0)) == "UNKNOWN"
