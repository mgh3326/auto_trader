import uuid

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.query_service import StageRunQueryService
from app.services.investment_stages.repository import InvestmentStagesRepository


@pytest.mark.asyncio
async def test_query_service_returns_artifacts_by_run(db_session):
    repo = InvestmentStagesRepository(db_session)
    bundle_uuid = uuid.uuid4()
    run = await repo.create_run(snapshot_bundle_uuid=bundle_uuid, market="kr")
    await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(
            stage_type="market", verdict=StageVerdict.NEUTRAL, confidence=10
        ),
    )
    await db_session.flush()

    svc = StageRunQueryService(db_session)
    result = await svc.get_run_with_artifacts(run.run_uuid)

    assert result is not None
    assert result.run.run_uuid == run.run_uuid
    assert len(result.artifacts) == 1
    assert result.artifacts[0].stage_type == "market"


@pytest.mark.asyncio
async def test_query_service_returns_none_for_missing_run(db_session):
    svc = StageRunQueryService(db_session)
    assert await svc.get_run_with_artifacts(uuid.uuid4()) is None
