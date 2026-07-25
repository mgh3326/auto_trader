"""ROB-602 Task 3: investment_watch_events_list_recent MCP 도구 테스트."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.investment_reports_handlers import (
    investment_watch_events_list_recent_impl,
)
from tests._watch_events_helpers import mk_watch_event, utc_at

pytestmark = pytest.mark.asyncio


async def test_tool_returns_delivered_events_json(session: AsyncSession):
    await mk_watch_event(session, symbol="KRW-TOOL", delivered_at=utc_at(0))
    await session.commit()
    out = await investment_watch_events_list_recent_impl(market="crypto", limit=50)
    assert out["success"] is True
    assert any(e["symbol"] == "KRW-TOOL" for e in out["events"])


async def test_tool_rejects_bad_timestamp():
    out = await investment_watch_events_list_recent_impl(since_timestamp="not-a-date")
    assert out["success"] is False
    assert out["error"] == "invalid_timestamp"
