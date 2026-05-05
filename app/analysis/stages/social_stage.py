# app/analysis/stages/social_stage.py
#
# DEPRECATED (ROB-115): SocialStageAnalyzer is no longer scheduled by
# the research pipeline. The class is kept for legacy data compatibility
# (existing stage_analysis rows with stage_type='social') and for
# potential re-introduction once a real social signal source is wired
# up. Do not add new callers.
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
