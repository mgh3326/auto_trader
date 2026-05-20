import uuid

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.repository import (
    AppendOnlyViolation,
    InvestmentStagesRepository,
)


@pytest.mark.asyncio
async def test_repository_creates_run_and_returns_uuid(db_session):
    repo = InvestmentStagesRepository(db_session)
    bundle_uuid = uuid.uuid4()
    run = await repo.create_run(
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
        policy_version="v1",
        generator_version="v1",
    )
    assert run.run_uuid is not None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_repository_persist_artifact_then_reject_overwrite(db_session):
    repo = InvestmentStagesRepository(db_session)
    bundle_uuid = uuid.uuid4()
    run = await repo.create_run(snapshot_bundle_uuid=bundle_uuid, market="kr")
    payload = StageArtifactPayload(
        stage_type="market",
        verdict=StageVerdict.NEUTRAL,
        confidence=50,
    )
    artifact = await repo.persist_artifact(run.run_uuid, payload)
    assert artifact.stage_type == "market"

    with pytest.raises(AppendOnlyViolation):
        await repo.persist_artifact(run.run_uuid, payload)


@pytest.mark.asyncio
async def test_repository_complete_run_sets_status(db_session):
    repo = InvestmentStagesRepository(db_session)
    run = await repo.create_run(snapshot_bundle_uuid=uuid.uuid4(), market="kr")
    await repo.complete_run(run.run_uuid, status="completed")
    refreshed = await repo.get_run(run.run_uuid)
    assert refreshed.status == "completed"
    assert refreshed.completed_at is not None
