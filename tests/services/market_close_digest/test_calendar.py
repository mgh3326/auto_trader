"""B2: crypto (and clamped KR/US) windows must not leave a permanent gap."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.market_close_digest.calendar import (
    infer_session_date,
    session_window,
    should_skip_holiday,
)
from app.services.market_close_digest.formatter import format_digest_message
from app.services.market_close_digest.types import DigestSnapshot
from tests.services.market_close_digest.fakes import AC1_SESSION_DATE, ac1_fills

pytestmark = pytest.mark.unit

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[3]


def test_crypto_cron_covers_previous_completed_kst_day() -> None:
    now = datetime(2026, 8, 20, 9, 5, tzinfo=KST)
    session_date = infer_session_date("crypto", now)
    assert session_date == date(2026, 8, 19)
    start, end = session_window("crypto", session_date, now=now)
    assert end <= now.astimezone(UTC)
    expected_start = datetime(2026, 8, 19, 0, 0, tzinfo=KST).astimezone(UTC)
    expected_end = datetime(2026, 8, 20, 0, 0, tzinfo=KST).astimezone(UTC)
    assert start == expected_start
    assert end == expected_end


def test_crypto_consecutive_runs_are_contiguous_and_keep_post_cron_fills() -> None:
    run_1 = datetime(2026, 8, 20, 9, 5, tzinfo=KST)
    run_2 = datetime(2026, 8, 21, 9, 5, tzinfo=KST)
    w1 = session_window("crypto", infer_session_date("crypto", run_1), now=run_1)
    w2 = session_window("crypto", infer_session_date("crypto", run_2), now=run_2)
    assert w1[1] == w2[0]

    fill_after_cron = datetime(2026, 8, 20, 15, 0, tzinfo=KST).astimezone(UTC)
    assert not (w1[0] <= fill_after_cron < w1[1])
    assert w2[0] <= fill_after_cron < w2[1]


def test_crypto_window_end_is_never_in_the_future() -> None:
    now = datetime(2026, 8, 20, 9, 5, tzinfo=KST)
    start, end = session_window("crypto", infer_session_date("crypto", now), now=now)
    assert end <= now.astimezone(UTC)
    assert start < end


def test_kr_and_us_windows_clamp_end_to_now() -> None:
    kr_now = datetime(2026, 8, 20, 15, 45, tzinfo=KST)
    kr_date = infer_session_date("kr", kr_now)
    assert kr_date == date(2026, 8, 20)
    _, kr_end = session_window("kr", kr_date, now=kr_now)
    assert kr_end == kr_now.astimezone(UTC)

    us_now = datetime(2026, 8, 20, 5, 5, tzinfo=KST)
    us_date = infer_session_date("us", us_now)
    assert us_date == AC1_SESSION_DATE
    _, us_end = session_window("us", us_date, now=us_now)
    assert us_end == us_now.astimezone(UTC)


def test_us_digest_at_0505_kst_uses_et_session_date() -> None:
    kst_now = datetime(2026, 8, 20, 5, 5, tzinfo=KST)
    assert infer_session_date("us", kst_now) == AC1_SESSION_DATE
    assert should_skip_holiday("us", AC1_SESSION_DATE) is False
    assert should_skip_holiday("us", date(2025, 7, 4)) is True
    assert should_skip_holiday("crypto", date(2025, 7, 4)) is False


def test_kis_fill_query_filters_equity_kr() -> None:
    source = (
        REPO / "app" / "services" / "market_close_digest" / "queries.py"
    ).read_text()
    assert (
        "KISLiveOrderLedger.instrument_type == InstrumentType.equity_kr.value" in source
    )


def test_formatter_says_net_notional_not_ladder() -> None:
    snapshot = DigestSnapshot(
        market="us",
        session_date=AC1_SESSION_DATE,
        status="ok",
        fills=ac1_fills(),
    )
    message = format_digest_message(snapshot)
    assert "순매수" in message
    assert "그물" not in message
