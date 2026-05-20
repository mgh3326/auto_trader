"""LLM Bear Reducer Stage (ROB-279)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.ai_providers.gemini_provider import GeminiProvider
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageContext

_logger = logging.getLogger(__name__)


class BearReducerStage:
    stage_type = "bear_reducer"

    def __init__(self, provider: GeminiProvider, budget: StageLLMBudget) -> None:
        self._provider = provider
        self._budget = budget

    async def run(self, context: StageContext) -> StageArtifactPayload:
        self._budget.consume(self.stage_type)

        # Collect prior evidence
        prior_details = []
        citations: list[StageCitation] = []
        for stype, art in context.prior_artifacts.items():
            prior_details.append(
                f"=== Stage: {stype} (Verdict: {art.verdict}, Confidence: {art.confidence}) ===\n"
                f"Summary: {art.summary}\n"
                f"Key Points: {art.key_points}\n"
                f"Risk Evidence: {art.risk_evidence}\n"
                f"Sell Evidence: {art.sell_evidence}\n"
            )
            for cite in art.cited_snapshots:
                citations.append(cite)

        prompt = (
            "You are a professional financial analyst. Your task is to synthesize the BEAR/RISK CASE "
            "based on the following intermediate stage analyses. You MUST reply ONLY with a valid "
            "JSON object containing the specified keys. Do not include markdown code blocks like ```json."
        )

        user_msg = (
            f"Intermediate Stage Outputs:\n"
            f"{chr(10).join(prior_details)}\n\n"
            f"Provide a JSON object conforming exactly to this schema:\n"
            f"{{\n"
            f'  "verdict": "bear" | "neutral" | "unavailable",\n'
            f'  "confidence": int (0 to 100),\n'
            f'  "summary": "str synthesizing the negative thesis",\n'
            f'  "key_points": ["point 1", "point 2"],\n'
            f'  "sell_evidence": ["evidence 1", "evidence 2"],\n'
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
                summary=data.get("summary", "Bear case synthesis completed"),
                key_points=data.get("key_points", []),
                sell_evidence=data.get("sell_evidence", []),
                risk_evidence=data.get("risk_evidence", []),
                cited_snapshots=citations,
                missing_data=data.get("missing_data", []),
                model_name=res.model,
                prompt_version="v1",
            )
        except Exception as exc:
            _logger.exception("Failed to run bear_reducer via LLM, falling back to deterministic neutral")
            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=StageVerdict.NEUTRAL,
                confidence=20,
                summary=f"Fallback synthesis (LLM failed: {exc!r})",
                cited_snapshots=citations,
                missing_data=[self.stage_type],
            )
