from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.operating_briefing import list_active_watches_impl
from app.mcp_server.tooling.operating_briefing_registration import (
    OPERATING_BRIEFING_TOOL_NAMES,
    register_operating_briefing_tools,
)
from app.models.investment_reports import InvestmentWatchAlert


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        assert description

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


def test_operating_briefing_tool_names_register() -> None:
    mcp = FakeMCP()

    register_operating_briefing_tools(mcp)  # type: ignore[arg-type]

    assert "list_active_watches" in OPERATING_BRIEFING_TOOL_NAMES
    assert "get_operating_briefing" in OPERATING_BRIEFING_TOOL_NAMES
    assert set(mcp.tools) == OPERATING_BRIEFING_TOOL_NAMES


@pytest.mark.asyncio
async def test_list_active_watches_impl_returns_rationale_and_filters(
    db_session: AsyncSession,
) -> None:
    future = datetime.now(tz=UTC) + timedelta(days=1)
    db_session.add(
        InvestmentWatchAlert(
            idempotency_key="rob517:list-active",
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            market="kr",
            target_kind="asset",
            symbol="005930",
            metric="price",
            operator="above",
            threshold=100000,
            threshold_key="price:above:100000",
            intent="trend_recovery_review",
            action_mode="notify_only",
            rationale="breakout watch",
            trigger_checklist=[{"check": "volume"}],
            max_action={},
            valid_until=future,
            status="active",
        )
    )
    await db_session.commit()

    result = await list_active_watches_impl(market="kr", symbol="005930")

    assert result["success"] is True
    assert result["count"] == 1
    assert result["filters"]["market"] == "kr"
    assert result["active_watches"][0]["symbol"] == "005930"
    assert result["active_watches"][0]["rationale"] == "breakout watch"
    assert result["active_watches"][0]["source_item_uuid"]
