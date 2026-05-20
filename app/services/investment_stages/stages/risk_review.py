"""LLM Risk Review Stage (ROB-279)."""

from __future__ import annotations

import json
import logging

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.budget import BudgetExceeded, StageLLMBudget
from app.services.investment_stages.stages.base import StageContext

_logger = logging.getLogger(__name__)


class RiskReviewStage:
    stage_type = "risk_review"

    def __init__(self, provider: object, budget: StageLLMBudget) -> None:
        self._provider = provider
        self._budget = budget

    async def run(self, context: StageContext) -> StageArtifactPayload:
        # Collect prior evidence first (before consuming budget)
        prior_details = []
        risk_lines: list[str] = []
        citations: list[StageCitation] = []
        for stype, art in context.prior_artifacts.items():
            prior_details.append(
                f"=== Stage: {stype} (Verdict: {art.verdict}, Confidence: {art.confidence}) ===\n"
                f"Summary: {art.summary}\n"
                f"Risk Evidence: {art.risk_evidence}\n"
            )
            risk_lines.extend(art.risk_evidence or [])
            for cite in art.cited_snapshots:
                citations.append(cite)

        try:
            self._budget.consume(self.stage_type)
        except BudgetExceeded:
            _logger.info("risk_review: budget exhausted, deterministic fallback")
            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=StageVerdict.BEAR if risk_lines else StageVerdict.NEUTRAL,
                confidence=40 if risk_lines else 20,
                summary="; ".join(risk_lines[:10]) or "no risk evidence",
                risk_evidence=risk_lines,
                cited_snapshots=citations,
                model_name=None,
                prompt_version=None,
            )

        prompt = (
            "You are a professional risk manager. Your task is to provide a FINAL RISK REVIEW "
            "based on the bull and bear case syntheses. Evaluate the overall risk-reward profile. "
            "You MUST reply ONLY with a valid JSON object containing the specified keys. "
            "Do not include markdown code blocks like ```json."
        )

        user_msg = (
            f"Intermediate Stage Outputs:\n"
            f"{chr(10).join(prior_details)}\n\n"
            f"Provide a JSON object conforming exactly to this schema:\n"
            f"{{\n"
            f'  "verdict": "bull" | "bear" | "neutral" | "unavailable",\n'
            f'  "confidence": int (0 to 100),\n'
            f'  "summary": "str synthesizing final risk verdict",\n'
            f'  "key_points": ["critical risk 1", "critical risk 2"],\n'
            f'  "risk_evidence": ["risk 1", "risk 2"],\n'
            f'  "missing_data": ["any absent data encountered"]\n'
            f"}}\n"
        )

        try:
            res = await self._provider.ask(system_prompt=prompt, user_message=user_msg)
            text = res.answer.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                text = "\n".join(lines).strip()

            data = json.loads(text)
            verdict_str = data.get("verdict", "neutral")
            verdict = StageVerdict(verdict_str)

            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=verdict,
                confidence=int(data.get("confidence", 50)),
                summary=data.get("summary", "Final risk review completed"),
                key_points=data.get("key_points", []),
                risk_evidence=data.get("risk_evidence", []),
                cited_snapshots=citations,
                missing_data=data.get("missing_data", []),
                model_name=res.model,
                prompt_version="v1",
            )
        except Exception as exc:
            _logger.exception("Failed to run risk_review via LLM, falling back to deterministic neutral")
            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=StageVerdict.NEUTRAL,
                confidence=20,
                summary=f"Fallback risk review (LLM failed: {exc!r})",
                cited_snapshots=citations,
                missing_data=[self.stage_type],
            )
