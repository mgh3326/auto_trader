from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun


@pytest.mark.asyncio
async def test_stage_run_insert_returns_uuid(db_session):
    run = InvestmentStageRun(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )
    db_session.add(run)
    await db_session.flush()
    assert run.run_uuid is not None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_stage_artifact_fk_cascade(db_session):
    run = InvestmentStageRun(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
    )
    db_session.add(run)
    await db_session.flush()

    artifact = InvestmentStageArtifact(
        run_uuid=run.run_uuid,
        stage_type="market",
        verdict="neutral",
        confidence=50,
        cited_snapshot_uuids=[],
    )
    db_session.add(artifact)
    await db_session.flush()

    fetched = await db_session.scalar(
        select(InvestmentStageArtifact).where(
            InvestmentStageArtifact.run_uuid == run.run_uuid
        )
    )
    assert fetched is not None
    assert fetched.stage_type == "market"
