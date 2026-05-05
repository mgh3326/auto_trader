# app/analysis/stages/social_stage.py
from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.schemas.research_pipeline import (
    SocialSignals,
    StageOutput,
    StageVerdict,
)


class SocialStageAnalyzer(BaseStageAnalyzer):
    stage_type = "social"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        return StageOutput(
            stage_type="social",
            verdict=StageVerdict.UNAVAILABLE,
            confidence=0,
            signals=SocialSignals(
                available=False, reason="not_implemented", phase="placeholder"
            ),
            source_freshness=None,
            model_name=None,
            prompt_version="social.placeholder.v1",
        )
