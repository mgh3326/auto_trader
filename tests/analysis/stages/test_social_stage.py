import pytest

from app.analysis.stages.base import StageContext
from app.analysis.stages.social_stage import SocialStageAnalyzer
from app.schemas.research_pipeline import SocialSignals, StageVerdict


@pytest.mark.unit
@pytest.mark.asyncio
async def test_social_stage_placeholder():
    stage = SocialStageAnalyzer()
    out = await stage.run(StageContext(session_id=1, symbol="X",
                                       instrument_type="equity_kr"))
    assert out.verdict == StageVerdict.UNAVAILABLE
    assert out.confidence == 0
    assert isinstance(out.signals, SocialSignals)
    assert out.signals.available is False
    assert out.signals.reason == "not_implemented"
    assert out.signals.phase == "placeholder"
