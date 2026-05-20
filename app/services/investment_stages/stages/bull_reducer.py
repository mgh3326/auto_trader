"""LLM Bull Reducer Stage (ROB-279)."""

from __future__ import annotations

from typing import Any

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.llm_reducer import (
    LLMReducerConfig,
    LLMReducerStage,
)

_BULL_CONFIG = LLMReducerConfig(
    stage_type="bull_reducer",
    system_prompt=(
        "You are a professional financial analyst. Synthesize the BULL CASE "
        "from intermediate stage analyses and reply only with valid JSON."
    ),
    schema_lines=(
        '  "verdict": "bull" | "neutral" | "unavailable",',
        '  "confidence": int (0 to 100),',
        '  "summary": "str synthesizing the positive thesis",',
        '  "key_points": ["point 1", "point 2"],',
        '  "buy_evidence": ["evidence 1", "evidence 2"],',
        '  "missing_data": ["any absent data encountered"]',
    ),
    detail_fields=("key_points", "buy_evidence"),
    evidence_fields=("buy_evidence",),
    fallback_fields=("buy_evidence",),
    evidence_verdict=StageVerdict.BULL,
    no_evidence_summary="no buy evidence",
    llm_default_summary="Bull case synthesis completed",
    llm_failure_summary_prefix="Fallback synthesis",
)


class BullReducerStage(LLMReducerStage):
    def __init__(self, provider: Any, budget: StageLLMBudget) -> None:
        super().__init__(provider, budget, _BULL_CONFIG)
