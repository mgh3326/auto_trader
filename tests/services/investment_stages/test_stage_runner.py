import uuid

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.stage_runner import StageRunner
from app.services.investment_stages.stages.base import (
    Stage,
    StageContext,
    UnavailableStageError,
)


class _MarketStub:
    stage_type = "market"

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        return StageArtifactPayload(
            stage_type="market", verdict=StageVerdict.BULL, confidence=70
        )


class _NewsUnavailable:
    stage_type = "news"

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        raise UnavailableStageError("no news snapshot")


class _StubBundleReadService:
    def __init__(self, bundle_uuid):
        self._bundle_uuid = bundle_uuid

    async def get_bundle(self, *, bundle_uuid):
        from types import SimpleNamespace
        return SimpleNamespace(
            bundle=SimpleNamespace(bundle_uuid=bundle_uuid, status="complete"),
            items=[],
        )


@pytest.mark.asyncio
async def test_stage_runner_runs_all_stages_and_persists(db_session):
    bundle_uuid = uuid.uuid4()
    runner = StageRunner(
        session=db_session,
        bundle_read_service=_StubBundleReadService(bundle_uuid),
        stages=[_MarketStub(), _NewsUnavailable()],
    )

    run = await runner.run(
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )

    assert run.status == "completed"
    artifacts = sorted(run.artifacts, key=lambda a: a.stage_type)
    assert [a.stage_type for a in artifacts] == ["market", "news"]
    assert artifacts[0].verdict == "bull"
    assert artifacts[1].verdict == "unavailable"
