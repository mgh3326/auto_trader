import uuid

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.candidate_universe import (
    CandidateUniverseStage,
)


class _Snap:
    def __init__(self, payload):
        self.snapshot_uuid = uuid.uuid4()
        self.payload_json = payload


def _ctx(payload):
    return StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"candidate_universe": [_Snap(payload)]},
        bundle_metadata={},
    )


@pytest.mark.asyncio
async def test_stage_bull_from_high_score_candidate():
    payload = {
        "freshness_status": "fresh",
        "source_coverage": {"tvscreener_upbit": 2},
        "candidates": [
            {
                "symbol": "KRW-BTC",
                "score": 8.5,
                "reasons": ["단기 상승 모멘텀 후보"],
                "source": "tvscreener_upbit",
            },
        ],
        "missing_data": None,
    }
    out = await CandidateUniverseStage().run(_ctx(payload))
    assert out.verdict == StageVerdict.BULL
    assert out.buy_evidence == ["KRW-BTC"]
    assert any("KRW-BTC" in kp for kp in out.key_points)
    assert out.confidence >= 40


@pytest.mark.asyncio
async def test_stage_stale_caps_confidence_and_sets_korean_missing_data():
    payload = {
        "freshness_status": "stale",
        "source_coverage": {"tvscreener_upbit": 1},
        "candidates": [
            {
                "symbol": "KRW-BTC",
                "score": 9.0,
                "reasons": ["단기 상승 모멘텀 후보"],
                "source": "tvscreener_upbit",
            },
        ],
        "missing_data": {
            "what": "암호화폐 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 (stale).",
            "why": "x",
            "next": "y",
            "confidence_impact": "cap 40",
        },
    }
    out = await CandidateUniverseStage().run(_ctx(payload))
    assert out.confidence <= 40
    assert out.missing_data and "stale" in out.missing_data[0]
    assert out.freshness_summary["candidate_universe"]["confidence_impact"] == "cap 40"


@pytest.mark.asyncio
async def test_stage_empty_is_neutral_low_confidence():
    payload = {
        "freshness_status": "missing",
        "source_coverage": {},
        "candidates": [],
        "missing_data": {
            "what": "암호화폐 스크리너 스냅샷이 비어 있습니다.",
            "why": "x",
            "next": "y",
            "confidence_impact": "cap 20",
        },
    }
    out = await CandidateUniverseStage().run(_ctx(payload))
    assert out.verdict == StageVerdict.NEUTRAL
    assert out.confidence == 20


@pytest.mark.asyncio
async def test_stage_missing_snapshot_raises():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={}
    )
    with pytest.raises(UnavailableStageError):
        await CandidateUniverseStage().run(ctx)
