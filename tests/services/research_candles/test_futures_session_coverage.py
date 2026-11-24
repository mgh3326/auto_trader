"""hk 6148: futures coverage (pure) and the ``--market futures`` diagnostic wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.research_candles.session_coverage import (
    StoredMinute,
    coverage_report,
    expected_minutes_by_segment,
)

pytestmark = pytest.mark.unit

KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 11, 24)


def _utc(hhmm: str) -> datetime:
    hour, minute = (int(p) for p in hhmm.split(":"))
    return datetime(2026, 11, 24, hour, minute, tzinfo=KST).astimezone(UTC)


def test_expected_minutes_are_the_day_session_only() -> None:
    expected = expected_minutes_by_segment("futures", "KRX", DAY)
    assert set(expected) == {"FUT_DAY"}
    assert len(expected["FUT_DAY"]) == 7 * 60  # 08:45-15:45
    assert _utc("08:45") in expected["FUT_DAY"]
    assert _utc("15:44") in expected["FUT_DAY"]
    assert _utc("15:45") not in expected["FUT_DAY"]


def test_coverage_counts_missing_minutes_and_latency() -> None:
    rows = [
        StoredMinute(
            symbol="MF200.2612",
            venue="KRX",
            session_segment="FUT_DAY",
            time_utc=_utc(f"09:{m:02d}"),
            retrieved_at=_utc(f"09:{m:02d}") + timedelta(seconds=75),
        )
        for m in (0, 1, 3)
    ]
    as_of = _utc("09:10")
    report = coverage_report(
        rows,
        market="futures",
        session_date=DAY,
        as_of=as_of,
        requested_symbols=["MF200.2612", "F200.2612"],
    )
    (entry,) = report["entries"]
    assert entry["session_segment"] == "FUT_DAY"
    assert entry["rows"] == 3
    # 08:45 .. 09:08 are closed at 09:10 (start + 70 s <= as_of): 24 minutes
    assert entry["expected_minutes"] == 24
    assert entry["missing_minutes"] == 21
    assert entry["latency_p50_s"] == 15.0
    assert report["symbols_without_rows"] == ["F200.2612"]
    assert report["night_collection"] == "unsupported"


def test_kr_and_us_reports_are_unchanged_by_the_futures_branch() -> None:
    report = coverage_report(
        [], market="kr", session_date=DAY, as_of=_utc("10:00")
    )
    assert report["venues_expected"] == ["KRX", "NTX"]
    assert report["night_collection"] is None


def test_diagnose_cli_accepts_the_futures_market_and_selects_the_futures_table() -> None:
    from scripts import diagnose_session_coverage as cli

    args = cli._parse_args(["--market", "futures", "--date", "2026-11-24"])
    assert args.market == "futures"
    assert "research.kr_futures_candles_1m" in str(cli._SELECT_FUTURES)
    assert "session_date = :session_date" in str(cli._SELECT_FUTURES)
    with pytest.raises(SystemExit):
        cli._parse_args(["--market", "options", "--date", "2026-11-24"])
