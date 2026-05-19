"""ROB-269 Phase 2 — SnapshotRefreshRequestService."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.models.investment_snapshots import InvestmentSnapshotRun
from app.schemas.investment_snapshots_mcp import RefreshRequest
from app.services.investment_snapshots.refresh_request_service import (
    SnapshotRefreshRequestService,
)


@pytest.mark.asyncio
async def test_record_inserts_manual_refresh_run(db_session):
    svc = SnapshotRefreshRequestService(db_session)
    response = await svc.record(
        RefreshRequest(
            reason="local smoke after deploy",
            market="kr",
            account_scope="kis_live",
        )
    )
    await db_session.commit()

    assert response.status == "running"

    run = await db_session.scalar(
        sa.select(InvestmentSnapshotRun).where(
            InvestmentSnapshotRun.run_uuid == response.run_uuid
        )
    )
    assert run is not None
    assert run.purpose == "manual_refresh"
    assert run.requested_by == "user"
    assert run.refresh_reason == "local smoke after deploy"
    assert run.policy_version == "intraday_action_report_v1"
    # Policy snapshot frozen onto the run.
    assert run.policy_snapshot_json["policy_version"] == "intraday_action_report_v1"
    # Filter metadata captured.
    assert run.run_metadata["symbols_filter"] is None
    assert run.run_metadata["snapshot_kinds_filter"] is None


@pytest.mark.asyncio
async def test_record_with_reviewer_requested_purpose_and_filters(db_session):
    svc = SnapshotRefreshRequestService(db_session)
    response = await svc.record(
        RefreshRequest(
            reason="reviewer wants fresh portfolio for PR re-check",
            purpose="reviewer_requested",
            market="kr",
            account_scope="kis_live",
            symbols=["035420", "005930"],
            snapshot_kinds=["portfolio"],
            requested_by="reviewer",
        )
    )
    await db_session.commit()

    run = await db_session.scalar(
        sa.select(InvestmentSnapshotRun).where(
            InvestmentSnapshotRun.run_uuid == response.run_uuid
        )
    )
    assert run is not None
    assert run.purpose == "reviewer_requested"
    assert run.requested_by == "reviewer"
    assert run.run_metadata["symbols_filter"] == ["035420", "005930"]
    assert run.run_metadata["snapshot_kinds_filter"] == ["portfolio"]


@pytest.mark.asyncio
async def test_record_unknown_policy_raises_keyerror(db_session):
    svc = SnapshotRefreshRequestService(db_session)
    with pytest.raises(KeyError, match="nonexistent_policy_v9"):
        await svc.record(
            RefreshRequest(
                reason="bad policy",
                market="kr",
                policy_version="nonexistent_policy_v9",
            )
        )
