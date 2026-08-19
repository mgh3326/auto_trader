from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app.services.market_close_digest.calendar import (
    infer_session_date,
    should_skip_holiday,
)
from app.services.market_close_digest.service import run_market_close_digest
from tests.services.market_close_digest.fakes import (
    AC1_SESSION_DATE,
    FakeDigestSources,
    ac1_fills,
    ac1_oversell_proposals,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_holiday_skips_without_send() -> None:
    notifier = AsyncMock()
    result = await run_market_close_digest(
        market="us",
        session_date=date(2025, 7, 4),
        sources=FakeDigestSources(fills=ac1_fills()),
        send=True,
        notifier=notifier,
    )
    assert result.status == "skipped_holiday"
    assert result.message == ""
    assert result.sent is False
    assert result.mutation_count == 0
    notifier.notify_agent_message.assert_not_called()


@pytest.mark.asyncio
async def test_zero_fills_sends_one_line() -> None:
    notifier = AsyncMock()
    notifier.notify_agent_message = AsyncMock(return_value=True)
    result = await run_market_close_digest(
        market="us",
        session_date=AC1_SESSION_DATE,
        sources=FakeDigestSources(),
        send=True,
        notifier=notifier,
    )
    assert result.status == "zero_fills"
    assert result.message == "US 마감 2026-08-19 · 체결 0건"
    assert result.sent is True
    assert result.mutation_count == 0
    notifier.notify_agent_message.assert_awaited_once()
    kwargs = notifier.notify_agent_message.await_args.kwargs
    assert kwargs["market_type"] == "us"
    assert kwargs["mirror_telegram"] is True
    assert kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_ac1_replay_counts() -> None:
    result = await run_market_close_digest(
        market="us",
        session_date=AC1_SESSION_DATE,
        sources=FakeDigestSources(
            fills=ac1_fills(),
            proposals=ac1_oversell_proposals(),
        ),
        send=False,
    )
    assert result.mutation_count == 0
    assert result.snapshot is not None
    assert result.snapshot.sell_count == 4
    assert result.snapshot.buy_count == 1
    assert len(result.snapshot.oversell_blocked) == 2
    assert result.snapshot.oversell_blocked[0].symbol == "AMZN"
    assert result.snapshot.oversell_blocked[1].symbol == "GOOGL"
    assert "차단 오버셀 2" in result.message
    assert result.snapshot.flags[0].startswith("오버셀 차단 2건")


@pytest.mark.asyncio
async def test_dry_run_does_not_call_notifier() -> None:
    notifier = AsyncMock()
    result = await run_market_close_digest(
        market="us",
        session_date=AC1_SESSION_DATE,
        sources=FakeDigestSources(fills=ac1_fills()),
        send=False,
        notifier=notifier,
    )
    assert result.sent is False
    notifier.notify_agent_message.assert_not_called()


def test_us_digest_at_0505_kst_uses_et_session_date() -> None:
    from zoneinfo import ZoneInfo

    kst_now = datetime(2026, 8, 20, 5, 5, tzinfo=ZoneInfo("Asia/Seoul"))
    assert infer_session_date("us", kst_now) == AC1_SESSION_DATE
    assert should_skip_holiday("us", AC1_SESSION_DATE) is False
    assert should_skip_holiday("us", date(2025, 7, 4)) is True
    assert should_skip_holiday("crypto", date(2025, 7, 4)) is False
