"""ROB-112 — Research pipeline read-only MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.models.research_pipeline import ResearchSession, ResearchSummary, StageAnalysis

logger = logging.getLogger(__name__)

async def research_session_get_impl(session_id: int) -> dict[str, Any]:
    """Returns 1 session + 4 latest stage rows + latest summary + cited stage_analysis ids."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ResearchSession)
                .where(ResearchSession.id == session_id)
                .options(
                    selectinload(ResearchSession.stage_analyses),
                    selectinload(ResearchSession.summaries).selectinload(ResearchSummary.stage_links),
                )
            )
            session = result.scalar_one_or_none()
            if not session:
                return _error_payload(
                    message=f"Research session {session_id} not found",
                    source="research_pipeline_read",
                    error_type="not_found"
                )

            # Map to dict
            data = {
                "id": session.id,
                "stock_info_id": session.stock_info_id,
                "research_run_id": session.research_run_id,
                "status": session.status,
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "finalized_at": session.finalized_at.isoformat() if session.finalized_at else None,
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "updated_at": session.updated_at.isoformat() if session.updated_at else None,
                "stage_analyses": [],
                "summaries": [],
            }

            for stage in session.stage_analyses:
                data["stage_analyses"].append({
                    "id": stage.id,
                    "stage_type": stage.stage_type,
                    "verdict": stage.verdict,
                    "confidence": stage.confidence,
                    "signals": stage.signals,
                    "executed_at": stage.executed_at.isoformat() if stage.executed_at else None,
                })

            for summary in session.summaries:
                data["summaries"].append({
                    "id": summary.id,
                    "decision": summary.decision,
                    "confidence": summary.confidence,
                    "reasons": summary.reasons,
                    "detailed_text": summary.detailed_text,
                    "executed_at": summary.executed_at.isoformat() if summary.executed_at else None,
                    "links": [
                        {
                            "stage_analysis_id": link.stage_analysis_id,
                            "weight": link.weight,
                            "direction": link.direction,
                        }
                        for link in summary.stage_links
                    ],
                })

            return data
    except Exception as exc:
        logger.error(f"research_session_get failed: {exc}")
        return _error_payload(message=str(exc), source="research_pipeline_read")

async def research_session_list_recent_impl(limit: int = 10) -> dict[str, Any]:
    """Returns recent N sessions with status, decision, confidence."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ResearchSession)
                .options(selectinload(ResearchSession.summaries))
                .order_by(ResearchSession.created_at.desc())
                .limit(limit)
            )
            sessions = result.scalars().all()

            items = []
            for s in sessions:
                latest_summary = sorted(s.summaries, key=lambda x: x.executed_at, reverse=True)[0] if s.summaries else None
                items.append({
                    "id": s.id,
                    "stock_info_id": s.stock_info_id,
                    "status": s.status,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "decision": latest_summary.decision if latest_summary else None,
                    "confidence": latest_summary.confidence if latest_summary else None,
                })

            return {"sessions": items}
    except Exception as exc:
        logger.error(f"research_session_list_recent failed: {exc}")
        return _error_payload(message=str(exc), source="research_pipeline_read")

async def stage_analysis_get_impl(stage_id: int) -> dict[str, Any]:
    """Returns one stage row by id."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(StageAnalysis).where(StageAnalysis.id == stage_id)
            )
            stage = result.scalar_one_or_none()
            if not stage:
                return _error_payload(
                    message=f"Stage analysis {stage_id} not found",
                    source="research_pipeline_read",
                    error_type="not_found"
                )

            return {
                "id": stage.id,
                "session_id": stage.session_id,
                "stage_type": stage.stage_type,
                "verdict": stage.verdict,
                "confidence": stage.confidence,
                "signals": stage.signals,
                "raw_payload": stage.raw_payload,
                "source_freshness": stage.source_freshness,
                "model_name": stage.model_name,
                "prompt_version": stage.prompt_version,
                "snapshot_at": stage.snapshot_at.isoformat() if stage.snapshot_at else None,
                "executed_at": stage.executed_at.isoformat() if stage.executed_at else None,
            }
    except Exception as exc:
        logger.error(f"stage_analysis_get failed: {exc}")
        return _error_payload(message=str(exc), source="research_pipeline_read")

async def research_summary_get_impl(summary_id: int) -> dict[str, Any]:
    """Returns one summary + linked stage rows by summary id."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ResearchSummary)
                .where(ResearchSummary.id == summary_id)
                .options(selectinload(ResearchSummary.stage_links))
            )
            summary = result.scalar_one_or_none()
            if not summary:
                return _error_payload(
                    message=f"Research summary {summary_id} not found",
                    source="research_pipeline_read",
                    error_type="not_found"
                )

            return {
                "id": summary.id,
                "session_id": summary.session_id,
                "decision": summary.decision,
                "confidence": summary.confidence,
                "bull_arguments": summary.bull_arguments,
                "bear_arguments": summary.bear_arguments,
                "price_analysis": summary.price_analysis,
                "reasons": summary.reasons,
                "detailed_text": summary.detailed_text,
                "warnings": summary.warnings,
                "model_name": summary.model_name,
                "prompt_version": summary.prompt_version,
                "executed_at": summary.executed_at.isoformat() if summary.executed_at else None,
                "links": [
                    {
                        "stage_analysis_id": link.stage_analysis_id,
                        "weight": link.weight,
                        "direction": link.direction,
                        "rationale": link.rationale,
                    }
                    for link in summary.stage_links
                ],
            }
    except Exception as exc:
        logger.error(f"research_summary_get failed: {exc}")
        return _error_payload(message=str(exc), source="research_pipeline_read")
