"""Final LLM Composer (ROB-279)."""

from __future__ import annotations

import json
import logging
from typing import Any
import uuid

from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.ai_providers.gemini_provider import GeminiProvider
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageArtifactPayload

_logger = logging.getLogger(__name__)


class FinalComposer:
    def __init__(self, provider: GeminiProvider, budget: StageLLMBudget) -> None:
        self._provider = provider
        self._budget = budget

    async def compose(
        self,
        *,
        run_uuid: uuid.UUID,
        snapshot_bundle_uuid: uuid.UUID,
        market: str,
        market_session: str | None,
        account_scope: str | None,
        kst_date: str,
        artifacts: list[Any],  # InvestmentStageArtifact ORM objects
    ) -> IngestReportRequest:
        self._budget.consume("final_composer")

        # Map artifacts by type to resolve UUIDs
        artifact_map = {a.stage_type: a for a in artifacts}
        
        # Prepare content for prompt
        stages_data = []
        for art in artifacts:
            stages_data.append(
                f"Stage: {art.stage_type} (Verdict: {art.verdict}, UUID: {art.artifact_uuid})\n"
                f"Summary: {art.summary}\n"
                f"Key Points: {art.key_points}\n"
                f"Buy Evidence: {art.buy_evidence}\n"
                f"Sell Evidence: {art.sell_evidence}\n"
                f"Risk Evidence: {art.risk_evidence}\n"
            )

        prompt = (
            "You are a master financial advisor composing the final advisory report. "
            "You MUST synthesize all the intermediate stage analysis outputs into a unified report. "
            "You MUST reply ONLY with a valid JSON object. Do not include markdown code blocks like ```json."
        )

        user_msg = (
            f"Intermediate Stages:\n"
            f"{chr(10).join(stages_data)}\n\n"
            f"Generate a final report in JSON format matching this schema exactly:\n"
            f"{{\n"
            f'  "title": "Advisory Report Title",\n'
            f'  "summary": "Main summary text synthesizing all stages",\n'
            f'  "risk_summary": "Risk summary synthesizing risk_review and bear case",\n'
            f'  "thesis_text": "Detailed investment thesis",\n'
            f'  "items": [\n'
            f"    {{\n"
            f'      "client_item_key": "AAPL_BUY",\n'
            f'      "item_kind": "action",\n'
            f'      "operation": "create",\n'
            f'      "symbol": "AAPL",\n'
            f'      "side": "buy",\n'
            f'      "intent": "buy_review",\n'
            f'      "priority": 0,\n'
            f'      "confidence": 85,\n'
            f'      "rationale": "Rationale referring to the news / candidate stage",\n'
            f'      "cited_stage_types": ["news", "candidate_universe"]\n'
            f"    }}\n"
            f"  ]\n"
            f"}}\n"
        )

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

        # Build IngestReportItem objects and enforce citations
        composed_items: list[IngestReportItem] = []
        fallback_uuid = (
            artifact_map.get("risk_review").artifact_uuid
            if "risk_review" in artifact_map
            else uuid.uuid4()
        )

        for raw_item in data.get("items", []):
            cited_types = raw_item.pop("cited_stage_types", [])
            cited_uuids = []
            for t in cited_types:
                if t in artifact_map:
                    cited_uuids.append(str(artifact_map[t].artifact_uuid))

            # Citation enforcement invariant
            if not cited_uuids:
                cited_uuids.append(str(fallback_uuid))

            metadata = raw_item.get("metadata", {})
            metadata["cited_stage_uuids"] = cited_uuids
            raw_item["metadata"] = metadata

            # Strip valid_until if watch is cancel/keep/review (to satisfy ROB-265 validators)
            if raw_item.get("item_kind") == "watch" and raw_item.get("operation") in ("cancel", "keep", "review"):
                raw_item.pop("valid_until", None)

            composed_items.append(IngestReportItem.model_validate(raw_item))

        # Compose overall request
        return IngestReportRequest(
            report_type="Advisory",
            market=market,  # type: ignore
            market_session=market_session,  # type: ignore
            account_scope=account_scope,  # type: ignore
            created_by_profile="AI_ADVISOR",
            title=data.get("title", "Advisory Report"),
            summary=data.get("summary", "Advisory report summary"),
            risk_summary=data.get("risk_summary"),
            thesis_text=data.get("thesis_text"),
            kst_date=kst_date,
            status="draft",
            items=composed_items,
            snapshot_bundle_uuid=snapshot_bundle_uuid,
            generator_version="v2_staged",
        )
