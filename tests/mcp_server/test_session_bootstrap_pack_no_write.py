from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling import session_bootstrap_pack as pack
from app.models.analysis_artifact import AnalysisArtifact
from app.models.investment_reports import InvestmentWatchAlert
from app.models.order_proposals import OrderProposal
from app.models.review import TradeForecast, TradeRetrospective
from app.models.session_context import OperatorSessionContext


def test_session_bootstrap_pack_has_no_write_or_mutation_references() -> None:
    path = (
        Path(__file__).parents[2] / "app/mcp_server/tooling/session_bootstrap_pack.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    source = path.read_text(encoding="utf-8")

    forbidden_calls = {"add", "commit", "flush", "delete"}
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
    }
    assert not calls
    for token in ("INSERT", "UPDATE", "DELETE"):
        assert token not in source
    for name in (
        "order_proposal_create",
        "order_proposal_void",
        "forecast_save",
        "investment_watch_create",
        "session_context_append",
        "analysis_artifact_save",
    ):
        assert name not in source


@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_session_bootstrap_pack_does_not_mutate_seeded_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual pack sources cannot write any seeded review projection."""

    now = datetime.now(tz=UTC)
    proposal_id = uuid.uuid4()
    db_session.add_all(
        [
            TradeForecast(
                created_by="codex",
                symbol="NOWRITE-FCAST",
                instrument_type="equity_kr",
                forecast_target={"kind": "no_resolvable_forecast"},
                probability=0.5,
                review_date=(now - timedelta(days=1)).date(),
                status="open",
            ),
            OperatorSessionContext(
                kst_date=pack.now_kst().date(),
                market="kr",
                account_scope=pack.operating_briefing._default_account_scope(
                    "kr", None
                ),
                entry_type="handoff_note",
                title="no-write context",
                body="seeded read-only context",
                refs={
                    "symbols": ["005930"],
                    "filled_notional": "70000",
                    "currency": "KRW",
                },
                created_by="codex",
            ),
            OrderProposal(
                proposal_id=proposal_id,
                root_proposal_id=proposal_id,
                symbol="NOWRITE-PROP",
                market="equity_kr",
                account_mode="kis_live",
                side="buy",
                order_type="limit",
                proposer="operator:bootstrap",
                lifecycle_state="proposed",
            ),
            TradeRetrospective(
                correlation_id=f"no-write-retro:{uuid.uuid4()}",
                symbol="NOWRITE-RETRO",
                instrument_type="equity_kr",
                account_mode="kis_live",
                market="kr",
                side="buy",
                outcome="filled",
                plan_price=70_000,
                fill_price=70_100,
                realized_pnl=100,
                realized_pnl_currency="KRW",
            ),
            InvestmentWatchAlert(
                idempotency_key=f"session-bootstrap-no-write:{uuid.uuid4()}",
                source_report_uuid=None,
                source_item_uuid=None,
                market="kr",
                target_kind="asset",
                symbol="005930",
                metric="price",
                operator="above",
                threshold=100_000,
                threshold_key="price:above:100000",
                intent="trend_recovery_review",
                action_mode="notify_only",
                rationale="read-only guard",
                trigger_checklist=[{"check": "volume"}],
                max_action={},
                valid_until=now + timedelta(days=1),
                status="active",
            ),
            AnalysisArtifact(
                market="kr",
                kind="briefing",
                title="no-write analysis",
                symbols=["005930"],
                payload={"price": 70_000, "currency": "KRW"},
                as_of=now,
                created_by="codex",
            ),
        ]
    )
    await db_session.commit()
    tables = (
        TradeForecast,
        OperatorSessionContext,
        OrderProposal,
        TradeRetrospective,
        InvestmentWatchAlert,
        AnalysisArtifact,
    )
    before = {
        table.__tablename__: await db_session.scalar(
            select(func.count()).select_from(table)
        )
        for table in tables
    }
    calls: list[bool] = []
    resolve = pack.forecast_tools.forecast_resolve

    async def spy_forecast_resolve(*, dry_run: bool, **kwargs: object) -> dict:
        calls.append(dry_run)
        return await resolve(dry_run=dry_run, **kwargs)

    monkeypatch.setattr(pack.forecast_tools, "forecast_resolve", spy_forecast_resolve)
    result = await pack._session_bootstrap_pack(
        "kr",
        ["resting", "pending_retros", "due_forecasts", "policy", "recent_context"],
        False,
        registered_tool_names=lambda: {
            "order_proposal_list",
            "trade_retrospective_pending",
            "forecast_resolve",
            "get_trading_policy",
            "session_context_get_recent",
        },
    )
    after = {
        table.__tablename__: await db_session.scalar(
            select(func.count()).select_from(table)
        )
        for table in tables
    }

    assert result["success"] is True
    assert after == before
    assert calls == [True]
