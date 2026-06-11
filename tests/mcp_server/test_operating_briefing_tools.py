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


@pytest.mark.asyncio
async def test_get_operating_briefing_composes_all_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from app.mcp_server.tooling import operating_briefing as ob

    async def fake_holdings(**kwargs):
        assert kwargs["market"] == "kr"
        assert kwargs["include_current_price"] is True
        return {
            "total_positions": 2,
            "summary": {"total_value": 1234567},
            "accounts": [
                {
                    "account": "kis",
                    "positions": [
                        {
                            "symbol": "005930",
                            "profit_rate": 3.2,
                            "profit_loss": 1000,
                            "evaluation_amount": 100000,
                        },
                        {
                            "symbol": "000660",
                            "profit_rate": -1.5,
                            "profit_loss": -500,
                            "evaluation_amount": 50000,
                        },
                    ],
                }
            ],
            "errors": [],
        }

    class FakePendingSnapshot:
        orders = [{"symbol": "005930", "expected_expiry": "2026-06-11T20:00:00+09:00"}]
        as_of = "2026-06-11T01:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        assert market == "kr"
        assert account_scope == "kis_live"
        return FakePendingSnapshot()

    async def fake_active_watches(**kwargs):
        return {
            "success": True,
            "count": 1,
            "as_of": datetime.now(tz=UTC).isoformat(),
            "filters": kwargs,
            "active_watches": [{"symbol": "005930", "rationale": "watch"}],
        }

    async def fake_latest_report(db, *, market, account_scope):
        return {
            "report_uuid": "11111111-1111-1111-1111-111111111111",
            "title": "latest plan",
            "status": "draft",
            "created_at": "2026-06-11T00:00:00+00:00",
            "items": {
                "total": 2,
                "by_status": {"approved": 1, "deferred": 1},
                "top": [{"symbol": "005930", "status": "approved"}],
            },
        }

    async def fake_session_context(db, *, market, account_scope, limit):
        return {
            "count": 1,
            "entries": [{"title": "handoff", "entry_type": "next_action"}],
        }

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)
    monkeypatch.setattr(ob, "list_active_watches_impl", fake_active_watches)
    monkeypatch.setattr(ob, "_latest_report_summary", fake_latest_report)
    monkeypatch.setattr(ob, "_recent_session_context", fake_session_context)

    result = await ob.get_operating_briefing_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["market"] == "kr"
    assert result["account_scope"] == "kis_live"
    assert result["holdings"]["summary"]["total_value"] == 1234567
    assert result["holdings"]["top_movers"][0]["symbol"] == "005930"
    assert result["pending_orders"]["orders"][0]["expected_expiry"].endswith("+09:00")
    assert result["active_watches"]["count"] == 1
    assert result["latest_report"]["title"] == "latest plan"
    assert result["session_context"]["entries"][0]["title"] == "handoff"
    assert result["staleness"]["pending_orders"]["freshness_status"] == "fresh"
