import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.candidate_universe import (
    CandidateUniverseStage,
)


def _snap(candidates):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="candidate_universe",
        payload_json={"candidates": candidates},
    )


@pytest.mark.asyncio
async def test_candidate_universe_bull_on_high_scores():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "candidate_universe": [
                _snap([{"symbol": "AAPL", "score": 8.5, "reason": "momentum"}])
            ]
        },
        bundle_metadata={},
    )
    payload = await CandidateUniverseStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert "AAPL" in payload.summary


@pytest.mark.asyncio
async def test_candidate_universe_neutral_on_low_scores():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "candidate_universe": [
                _snap([{"symbol": "INTC", "score": 2.0, "reason": "laggard"}])
            ]
        },
        bundle_metadata={},
    )
    payload = await CandidateUniverseStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
