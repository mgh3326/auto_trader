"""LLM Risk Review Stage (ROB-279)."""

from __future__ import annotations

from typing import Any

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.llm_reducer import (
    LLMReducerConfig,
    LLMReducerStage,
)

_RISK_CONFIG = LLMReducerConfig(
    stage_type="risk_review",
    system_prompt=(
        "You are a professional risk manager. Provide a final risk review "
        "from bull and bear syntheses and reply only with valid JSON."
    ),
    schema_lines=(
        '  "verdict": "bull" | "bear" | "neutral" | "unavailable",',
        '  "confidence": int (0 to 100),',
        '  "summary": "str synthesizing final risk verdict",',
        '  "key_points": ["critical risk 1", "critical risk 2"],',
        '  "risk_evidence": ["risk 1", "risk 2"],',
        '  "missing_data": ["any absent data encountered"]',
    ),
    detail_fields=("risk_evidence",),
    evidence_fields=("risk_evidence",),
    fallback_fields=("risk_evidence",),
    evidence_verdict=StageVerdict.BEAR,
    no_evidence_summary="no risk evidence",
    llm_default_summary="Final risk review completed",
    llm_failure_summary_prefix="Fallback risk review",
)


class RiskReviewStage(LLMReducerStage):
    def __init__(self, provider: Any, budget: StageLLMBudget) -> None:
        super().__init__(provider, budget, _RISK_CONFIG)
