from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.operating_briefing import list_active_watches_impl
from app.mcp_server.tooling.operating_briefing_registration import (
    OPERATING_BRIEFING_TOOL_NAMES,
    register_operating_briefing_tools,
)
from app.models.investment_reports import InvestmentWatchAlert
from app.services.investment_reports.repository import InvestmentReportsRepository


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        assert description

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


async def _insert_briefing_report(
    session: AsyncSession,
    *,
    title: str,
    created_by_profile: str,
    status: str = "draft",
):
    repo = InvestmentReportsRepository(session)
    return await repo.insert_report(
        idempotency_key=f"rob520:briefing-report:{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile=created_by_profile,
        title=title,
        summary="s",
        status=status,
        report_metadata={},
    )


def test_operating_briefing_tool_names_register() -> None:
    mcp = FakeMCP()

    register_operating_briefing_tools(mcp)  # type: ignore[arg-type]

    assert "list_active_watches" in OPERATING_BRIEFING_TOOL_NAMES
    assert "get_operating_briefing" in OPERATING_BRIEFING_TOOL_NAMES
    assert set(mcp.tools) == OPERATING_BRIEFING_TOOL_NAMES


@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_list_active_watches_impl_returns_rationale_and_filters(
    db_session: AsyncSession,
) -> None:
    future = datetime.now(tz=UTC) + timedelta(days=1)
    db_session.add(
        InvestmentWatchAlert(
            idempotency_key=f"rob517:list-active:{uuid.uuid4()}",
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
    assert result["count"] >= 1
    assert result["filters"]["market"] == "kr"
    matching = [
        w for w in result["active_watches"] if w["rationale"] == "breakout watch"
    ]
    assert len(matching) >= 1
    assert matching[0]["symbol"] == "005930"
    assert matching[0]["source_item_uuid"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section_name", "patch_name"),
    [
        ("latest_report", "_latest_report_summary"),
        ("session_context", "_recent_session_context"),
        ("active_watches", "list_active_watches_impl"),
    ],
)
async def test_get_operating_briefing_fail_opens_optional_sections(
    monkeypatch: pytest.MonkeyPatch,
    section_name: str,
    patch_name: str,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    async def fake_holdings(**kwargs):
        return {
            "filters": {"market": kwargs["market"]},
            "total_accounts": 1,
            "total_positions": 0,
            "summary": {},
            "accounts": [],
            "errors": [],
        }

    class EmptyPendingSnapshot:
        orders: list[dict] = []
        as_of = "2026-06-11T01:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        return EmptyPendingSnapshot()

    async def ok_latest_report(db, *, market, account_scope):
        return {
            "report_uuid": "11111111-1111-1111-1111-111111111111",
            "title": "latest plan",
            "status": "draft",
            "created_at": "2026-06-11T00:00:00+00:00",
            "items": {"total": 0, "by_status": {}, "top": []},
        }

    async def ok_session_context(db, *, market, account_scope, limit):
        return {"count": 1, "entries": [{"title": "handoff"}]}

    async def ok_active_watches(**kwargs):
        return {
            "success": True,
            "count": 1,
            "as_of": "2026-06-11T01:00:00+00:00",
            "filters": kwargs,
            "active_watches": [{"symbol": "005930"}],
        }

    async def boom(*args, **kwargs):
        raise RuntimeError("section boom")

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)
    monkeypatch.setattr(ob, "_latest_report_summary", ok_latest_report)
    monkeypatch.setattr(ob, "_recent_session_context", ok_session_context)
    monkeypatch.setattr(ob, "list_active_watches_impl", ok_active_watches)
    monkeypatch.setattr(ob, patch_name, boom)

    result = await ob.get_operating_briefing_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["staleness"][section_name]["freshness_status"] == "unavailable"
    assert result["staleness"][section_name]["unavailable_reason"].startswith(
        f"{section_name}_failed:RuntimeError:section boom"
    )
    if section_name == "latest_report":
        assert result["latest_report"] is None
    elif section_name == "session_context":
        assert result["session_context"] == {
            "count": 0,
            "entries": [],
            "unavailable_reason": "session_context_failed:RuntimeError:section boom",
        }
    else:
        assert result["active_watches"] == {
            "count": 0,
            "watches": [],
            "unavailable_reason": "active_watches_failed:RuntimeError:section boom",
        }


@pytest.mark.asyncio
async def test_latest_report_summary_skips_non_advisory_newer_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    advisory_report = SimpleNamespace(
        report_uuid=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        title="latest advisory",
        status="draft",
        created_at=datetime(2026, 6, 11, 1, 0, tzinfo=UTC),
        created_by_profile="CLAUDE_ADVISOR",
    )
    advisory_item = SimpleNamespace(
        item_uuid=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        symbol="005930",
        item_kind="action",
        intent="buy_review",
        status="approved",
        rationale="advisory item",
    )

    class FakeQueryService:
        def __init__(self, db):
            self.db = db

        async def latest_report(self, **kwargs):
            assert kwargs["market"] == "kr"
            assert kwargs["account_scope"] == "kis_live"
            assert kwargs["created_by_profiles"] == {"HERMES_ADVISOR", "CLAUDE_ADVISOR"}
            assert kwargs["exclude_statuses"] == {"superseded"}
            return advisory_report

        async def get_bundle(self, report_uuid):
            assert report_uuid == advisory_report.report_uuid
            return {"items": [advisory_item]}

    monkeypatch.setattr(ob, "InvestmentReportQueryService", FakeQueryService)

    summary = await ob._latest_report_summary(
        object(),
        market="kr",
        account_scope="kis_live",
    )

    assert summary is not None
    assert summary["report_uuid"] == str(advisory_report.report_uuid)
    assert summary["title"] == "latest advisory"
    assert summary["items"]["by_status"] == {"approved": 1}


@pytest.mark.asyncio
async def test_latest_report_summary_finds_advisory_beyond_twenty_smoke_reports(
    session: AsyncSession,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    advisory = await _insert_briefing_report(
        session,
        title="older advisory",
        created_by_profile="CLAUDE_ADVISOR",
    )
    for idx in range(25):
        await _insert_briefing_report(
            session,
            title=f"newer smoke {idx}",
            created_by_profile="test",
        )
    await session.commit()

    summary = await ob._latest_report_summary(
        session,
        market="kr",
        account_scope="kis_mock",
    )

    assert summary is not None
    assert summary["report_uuid"] == str(advisory.report_uuid)
    assert summary["title"] == "older advisory"


@pytest.mark.asyncio
async def test_latest_report_summary_excludes_superseded_advisory(
    session: AsyncSession,
) -> None:
    from app.mcp_server.tooling import operating_briefing as ob

    current = await _insert_briefing_report(
        session,
        title="current advisory",
        created_by_profile="CLAUDE_ADVISOR",
    )
    await _insert_briefing_report(
        session,
        title="superseded advisory",
        created_by_profile="CLAUDE_ADVISOR",
        status="superseded",
    )
    await session.commit()

    summary = await ob._latest_report_summary(
        session,
        market="kr",
        account_scope="kis_mock",
    )

    assert summary is not None
    assert summary["report_uuid"] == str(current.report_uuid)
    assert summary["title"] == "current advisory"


@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_get_operating_briefing_reads_active_watch_and_session_context(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.mcp_server.tooling import operating_briefing as ob
    from app.mcp_server.tooling.session_context_tools import session_context_append

    async def fake_holdings(**kwargs):
        return {
            "filters": {"market": kwargs["market"]},
            "total_accounts": 1,
            "total_positions": 0,
            "summary": {},
            "accounts": [],
            "errors": [],
        }

    class EmptyPendingSnapshot:
        orders: list[dict] = []
        as_of = "2026-06-11T01:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        return EmptyPendingSnapshot()

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)

    db_session.add(
        InvestmentWatchAlert(
            idempotency_key=f"rob517:briefing-active:{uuid.uuid4()}",
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            market="us",
            target_kind="asset",
            symbol="AAPL",
            metric="price",
            operator="above",
            threshold=100000,
            threshold_key="price:above:100000",
            intent="trend_recovery_review",
            action_mode="notify_only",
            rationale="briefing watch",
            trigger_checklist=[],
            max_action={},
            valid_until=datetime.now(tz=UTC) + timedelta(days=1),
            status="active",
        )
    )
    await db_session.commit()
    await session_context_append(
        entries=[
            {
                "kst_date": "2026-06-11",
                "market": "us",
                "account_scope": "kis_live",
                "entry_type": "next_action",
                "title": "재평가",
                "body": "내일 20:00 만료 주문 재평가",
            }
        ]
    )

    result = await ob.get_operating_briefing_impl(
        market="us",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["active_watches"]["count"] >= 1
    matching_watches = [
        w
        for w in result["active_watches"]["watches"]
        if w["rationale"] == "briefing watch"
    ]
    assert len(matching_watches) >= 1
    assert result["session_context"]["count"] >= 1
    matching_entries = [
        e for e in result["session_context"]["entries"] if e["title"] == "재평가"
    ]
    assert len(matching_entries) >= 1
