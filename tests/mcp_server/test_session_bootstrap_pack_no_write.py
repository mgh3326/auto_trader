from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling import session_bootstrap_pack as pack
from app.models.investment_reports import InvestmentWatchAlert


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
    """A pack call leaves a real, seeded review table unchanged."""

    db_session.add(
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
            valid_until=datetime.now(tz=UTC) + timedelta(days=1),
            status="active",
        )
    )
    await db_session.commit()
    before = await db_session.scalar(
        select(func.count()).select_from(InvestmentWatchAlert)
    )

    async def source(*args: object, **kwargs: object) -> dict[str, object]:
        return {"success": True, "entries": []}

    monkeypatch.setattr(pack, "_section_source", source)
    result = await pack._session_bootstrap_pack(
        "kr",
        ["recent_context"],
        False,
        registered_tool_names=lambda: {"session_context_get_recent"},
    )
    after = await db_session.scalar(
        select(func.count()).select_from(InvestmentWatchAlert)
    )

    assert result["success"] is True
    assert after == before
