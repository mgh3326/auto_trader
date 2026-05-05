"""ROB-112 — Research pipeline service."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analysis.pipeline import run_research_session
from app.core.db import AsyncSessionLocal
from app.models.research_pipeline import ResearchSession, ResearchSummary, StageAnalysis
from app.schemas.research_pipeline import ResearchSessionCreateResponse
from app.services.stock_info_service import create_stock_if_not_exists

logger = logging.getLogger(__name__)


class ResearchPipelineService:
    """Service to wrap and export research pipeline functionality."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def run_session(
        self,
        symbol: str,
        name: str,
        instrument_type: str,
        research_run_id: int | None = None,
        user_id: int | None = None,
    ) -> int:
        """
        Runs a research session for the given symbol.
        """
        return await run_research_session(
            db=self.db,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type,
            research_run_id=research_run_id,
            user_id=user_id,
        )

    async def create_session_and_dispatch(
        self,
        *,
        symbol: str,
        name: str | None,
        instrument_type: str,
        research_run_id: int | None,
        user_id: int | None,
    ) -> ResearchSessionCreateResponse:
        stock_info = await create_stock_if_not_exists(
            symbol=symbol,
            name=name or symbol,
            instrument_type=instrument_type,
            db=self.db,
        )
        session = ResearchSession(
            stock_info_id=stock_info.id,
            research_run_id=research_run_id,
            status="open",
            started_at=datetime.now(UTC),
        )
        self.db.add(session)
        await self.db.flush()
        await self.db.commit()
        session_id = session.id

        asyncio.create_task(
            _run_session_in_background(
                session_id=session_id,
                symbol=symbol,
                name=name or symbol,
                instrument_type=instrument_type,
                research_run_id=research_run_id,
                user_id=user_id,
            )
        )

        return ResearchSessionCreateResponse(
            session_id=session_id,
            status="running",
            started_at=session.started_at,
        )

    async def list_recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Returns recent N sessions with status, decision, confidence."""
        result = await self.db.execute(
            select(ResearchSession)
            .options(selectinload(ResearchSession.summaries))
            .order_by(ResearchSession.created_at.desc())
            .limit(limit)
        )
        sessions = result.scalars().all()

        items = []
        for s in sessions:
            latest_summary = (
                max(s.summaries, key=lambda x: x.executed_at) if s.summaries else None
            )
            items.append(
                {
                    "id": s.id,
                    "stock_info_id": s.stock_info_id,
                    "status": s.status,
                    "created_at": s.created_at,
                    "decision": latest_summary.decision if latest_summary else None,
                    "confidence": latest_summary.confidence if latest_summary else None,
                }
            )

        return items

    async def get_session(self, session_id: int) -> dict[str, Any] | None:
        """Returns session header + status."""
        result = await self.db.execute(
            select(ResearchSession).where(ResearchSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            return None

        return {
            "id": session.id,
            "stock_info_id": session.stock_info_id,
            "research_run_id": session.research_run_id,
            "status": session.status,
            "started_at": session.started_at,
            "finalized_at": session.finalized_at,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }

    async def get_session_full(self, session_id: int) -> dict[str, Any] | None:
        """Returns session header with symbol + instrument_type, all stages, and latest summary."""
        from app.models.analysis import StockInfo

        session_result = await self.db.execute(
            select(ResearchSession, StockInfo)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .where(ResearchSession.id == session_id)
        )
        row = session_result.first()
        if not row:
            return None
        session, stock_info = row

        stages = await self.get_latest_stages(session_id)
        summary = await self.get_latest_summary(session_id)

        return {
            "session": {
                "id": session.id,
                "stock_info_id": session.stock_info_id,
                "research_run_id": session.research_run_id,
                "status": session.status,
                "started_at": session.started_at,
                "finalized_at": session.finalized_at,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "symbol": stock_info.symbol,
                "instrument_type": stock_info.instrument_type,
            },
            "stages": stages,
            "summary": summary,
        }

    async def get_latest_stages(self, session_id: int) -> list[dict[str, Any]]:
        """Returns latest stage row per stage_type."""
        # Use DISTINCT ON or manual grouping to get latest per stage_type
        # For simplicity and compatibility, we'll fetch all and group in Python
        # as there are only 4 types.
        result = await self.db.execute(
            select(StageAnalysis)
            .where(StageAnalysis.session_id == session_id)
            .order_by(StageAnalysis.stage_type, StageAnalysis.executed_at.desc())
        )
        stages = result.scalars().all()

        latest_stages = {}
        for stage in stages:
            if stage.stage_type not in latest_stages:
                latest_stages[stage.stage_type] = {
                    "id": stage.id,
                    "stage_type": stage.stage_type,
                    "verdict": stage.verdict,
                    "confidence": stage.confidence,
                    "signals": stage.signals,
                    "raw_payload": stage.raw_payload,
                    "source_freshness": stage.source_freshness,
                    "executed_at": stage.executed_at,
                    "snapshot_at": stage.snapshot_at,
                }

        return list(latest_stages.values())

    async def get_latest_summary(self, session_id: int) -> dict[str, Any] | None:
        """Returns latest summary + cited stage_analysis ids."""
        result = await self.db.execute(
            select(ResearchSummary)
            .where(
                ResearchSummary.id
                == (
                    select(ResearchSummary.id)
                    .where(ResearchSummary.session_id == session_id)
                    .order_by(ResearchSummary.executed_at.desc())
                    .limit(1)
                    .scalar_subquery()
                )
            )
            .options(selectinload(ResearchSummary.stage_links))
        )
        summary = result.scalar_one_or_none()
        if not summary:
            return None

        stage_ids = [link.stage_analysis_id for link in summary.stage_links]
        stage_type_by_id: dict[int, str] = {}
        if stage_ids:
            stage_rows = await self.db.execute(
                select(StageAnalysis).where(StageAnalysis.id.in_(stage_ids))
            )
            for stage in stage_rows.scalars().all():
                stage_type_by_id[stage.id] = stage.stage_type

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
            "executed_at": summary.executed_at,
            "cited_stage_analysis_ids": stage_ids,
            "summary_stage_links": [
                {
                    "stage_analysis_id": link.stage_analysis_id,
                    "stage_type": stage_type_by_id.get(link.stage_analysis_id, "unknown"),
                    "direction": link.direction,
                    "weight": link.weight,
                    "rationale": link.rationale,
                }
                for link in summary.stage_links
            ],
        }


async def _run_session_in_background(
    *,
    session_id: int,
    symbol: str,
    name: str,
    instrument_type: str,
    research_run_id: int | None,
    user_id: int | None,
) -> None:
    from app.analysis.pipeline import run_research_session as _run_research_session

    async with AsyncSessionLocal() as db:
        try:
            await _run_research_session(
                db=db,
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
                research_run_id=research_run_id,
                user_id=user_id,
                existing_session_id=session_id,
            )
        except Exception:
            logger.exception(
                "research pipeline background run failed session_id=%s", session_id
            )
            try:
                from sqlalchemy import update as sa_update

                await db.execute(
                    sa_update(ResearchSession)
                    .where(ResearchSession.id == session_id)
                    .values(status="failed", finalized_at=datetime.now(UTC))
                )
                await db.commit()
            except Exception:
                logger.exception("failed to mark session_id=%s failed", session_id)
