"""Shared implementation for LLM-backed reducer stages."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.budget import BudgetExceeded, StageLLMBudget
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.llm_utils import (
    collect_prior_stage_inputs,
    ensure_json_mapping,
    strip_markdown_fence,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMReducerConfig:
    stage_type: str
    system_prompt: str
    schema_lines: Sequence[str]
    detail_fields: Sequence[str]
    evidence_fields: Sequence[str]
    fallback_fields: Sequence[str]
    evidence_verdict: StageVerdict
    no_evidence_summary: str
    llm_default_summary: str
    llm_failure_summary_prefix: str


class LLMReducerStage:
    """Common budget, prompt, parse, and fallback behavior for LLM reducers."""

    stage_type: str

    def __init__(
        self, provider: Any, budget: StageLLMBudget, config: LLMReducerConfig
    ) -> None:
        self._provider = provider
        self._budget = budget
        self._config = config
        self.stage_type = config.stage_type

    async def run(self, context: StageContext) -> StageArtifactPayload:
        prior_details, evidence, citations = collect_prior_stage_inputs(
            context,
            evidence_fields=self._config.evidence_fields,
            detail_fields=self._config.detail_fields,
        )
        fallback_lines = self._fallback_lines(evidence)

        try:
            self._budget.consume(self.stage_type)
        except BudgetExceeded:
            _logger.info(
                "%s: budget exhausted, deterministic fallback", self.stage_type
            )
            return self._fallback_payload(
                evidence=evidence,
                fallback_lines=fallback_lines,
                citations=citations,
            )

        try:
            res = await self._provider.ask(
                system_prompt=self._config.system_prompt,
                user_message=self._user_message(prior_details),
            )
            data = ensure_json_mapping(json.loads(strip_markdown_fence(res.answer)))
            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=StageVerdict(data.get("verdict", "neutral")),
                confidence=int(data.get("confidence", 50)),
                summary=data.get("summary", self._config.llm_default_summary),
                key_points=data.get("key_points", []),
                cited_snapshots=citations,
                missing_data=data.get("missing_data", []),
                model_name=res.model,
                prompt_version="v1",
                buy_evidence=self._stage_evidence_values(data, "buy_evidence"),
                sell_evidence=self._stage_evidence_values(data, "sell_evidence"),
                risk_evidence=self._stage_evidence_values(data, "risk_evidence"),
            )
        except Exception as exc:
            _logger.exception(
                "Failed to run %s via LLM, falling back to deterministic neutral",
                self.stage_type,
            )
            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=StageVerdict.NEUTRAL,
                confidence=20,
                summary=f"{self._config.llm_failure_summary_prefix} (LLM failed: {exc!r})",
                cited_snapshots=citations,
                missing_data=[self.stage_type],
            )

    def _user_message(self, prior_details: Sequence[str]) -> str:
        schema = "\n".join(self._config.schema_lines)
        details = "\n".join(prior_details)
        return (
            "Intermediate Stage Outputs:\n"
            f"{details}\n\n"
            "Provide a JSON object conforming exactly to this schema:\n"
            "{\n"
            f"{schema}\n"
            "}\n"
        )

    def _fallback_payload(
        self,
        *,
        evidence: dict[str, list[str]],
        fallback_lines: list[str],
        citations: list[StageCitation],
    ) -> StageArtifactPayload:
        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=self._config.evidence_verdict
            if fallback_lines
            else StageVerdict.NEUTRAL,
            confidence=40 if fallback_lines else 20,
            summary="; ".join(fallback_lines[:10]) or self._config.no_evidence_summary,
            cited_snapshots=citations,
            model_name=None,
            prompt_version=None,
            buy_evidence=self._stage_evidence_values(evidence, "buy_evidence"),
            sell_evidence=self._stage_evidence_values(evidence, "sell_evidence"),
            risk_evidence=self._stage_evidence_values(evidence, "risk_evidence"),
        )

    def _fallback_lines(self, evidence: dict[str, list[str]]) -> list[str]:
        for field in self._config.fallback_fields:
            if evidence[field]:
                return evidence[field]
        return []

    def _stage_evidence_values(self, data: dict[str, Any], field: str) -> list[str]:
        values = data.get(field, [])
        return values if isinstance(values, list) else []
