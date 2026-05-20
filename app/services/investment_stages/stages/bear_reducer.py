"""LLM Bear Reducer Stage (ROB-279)."""

from __future__ import annotations

from typing import Any

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.llm_reducer import (
    LLMReducerConfig,
    LLMReducerStage,
)

_BEAR_CONFIG = LLMReducerConfig(
    stage_type="bear_reducer",
    system_prompt=(
        "You are a professional financial analyst. Synthesize the BEAR/RISK CASE "
        "from intermediate stage analyses and reply only with valid JSON."
    ),
    schema_lines=(
        '  "verdict": "bear" | "neutral" | "unavailable",',
        '  "confidence": int (0 to 100),',
        '  "summary": "str synthesizing the negative thesis",',
        '  "key_points": ["point 1", "point 2"],',
        '  "sell_evidence": ["evidence 1", "evidence 2"],',
        '  "risk_evidence": ["risk 1", "risk 2"],',
        '  "missing_data": ["any absent data encountered"]',
    ),
    detail_fields=("key_points", "risk_evidence", "sell_evidence"),
    evidence_fields=("sell_evidence", "risk_evidence"),
    fallback_fields=("sell_evidence", "risk_evidence"),
    evidence_verdict=StageVerdict.BEAR,
    no_evidence_summary="no bear evidence",
    llm_default_summary="Bear case synthesis completed",
    llm_failure_summary_prefix="Fallback synthesis",
)


class BearReducerStage(LLMReducerStage):
    def __init__(self, provider: Any, budget: StageLLMBudget) -> None:
        super().__init__(provider, budget, _BEAR_CONFIG)
