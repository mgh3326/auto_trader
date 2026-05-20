import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.market import MarketStage


def _snapshot(payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="market",
        payload_json=payload,
    )


@pytest.mark.asyncio
async def test_market_stage_emits_bull_when_index_up():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": 2.0}}})]
        },
        bundle_metadata={},
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence >= 50
    assert len(payload.cited_snapshots) == 1


@pytest.mark.asyncio
async def test_market_stage_emits_bear_when_index_down():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": -2.0}}})]
        },
        bundle_metadata={},
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BEAR


@pytest.mark.asyncio
async def test_market_stage_raises_unavailable_when_no_snapshot():
    ctx = StageContext(bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={})
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)
