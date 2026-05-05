"""ROB-112 — Research pipeline orchestrator."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.debate import build_summary
from app.analysis.stages.base import StageContext
from app.analysis.stages.fundamentals_stage import FundamentalsStageAnalyzer
from app.analysis.stages.market_stage import MarketStageAnalyzer
from app.analysis.stages.news_stage import NewsStageAnalyzer
from app.analysis.stages.social_stage import SocialStageAnalyzer
from app.core.config import settings
from app.models.research_pipeline import (
    ResearchSession,
    ResearchSummary,
    StageAnalysis,
    SummaryStageLink,
)
from app.services.legacy_stock_analysis_adapter import LegacyStockAnalysisAdapter
from app.services.stock_info_service import create_stock_if_not_exists

logger = logging.getLogger(__name__)

async def run_research_session(
    db: AsyncSession,
    symbol: str,
    name: str,
    instrument_type: str,
    research_run_id: int | None = None,
    user_id: int | None = None,
) -> int:
    """
    Orchestrates the entire research pipeline: session creation,
    parallel stage execution, result persistence, and summary generation.
    """

    # 1. create_stock_if_not_exists
    stock_info = await create_stock_if_not_exists(
        symbol=symbol,
        name=name,
        instrument_type=instrument_type,
        db=db,
    )

    # 2. Insert ResearchSession(status='open')
    session = ResearchSession(
        stock_info_id=stock_info.id,
        research_run_id=research_run_id,
        status="open",
        started_at=datetime.now(UTC),
    )
    db.add(session)
    await db.flush()
    session_id = session.id

    # 3. Run 4 stage analyzers concurrently via asyncio.gather
    ctx = StageContext(
        session_id=session_id,
        symbol=symbol,
        instrument_type=instrument_type,
        user_id=user_id,
    )

    analyzers = [
        MarketStageAnalyzer(),
        NewsStageAnalyzer(),
        FundamentalsStageAnalyzer(),
        SocialStageAnalyzer(),
    ]

    # Run analyzers concurrently
    stage_results = await asyncio.gather(
        *(analyzer.run(ctx) for analyzer in analyzers),
        return_exceptions=True
    )

    # 4. Validate each StageOutput, insert StageAnalysis row, capture DB id
    stage_outputs_map = {}
    for res in stage_results:
        if isinstance(res, Exception):
            logger.error(f"Stage analyzer failed: {res}")
            # We continue to allow partial results
            continue

        # Insert StageAnalysis row
        stage_analysis = StageAnalysis(
            session_id=session_id,
            stage_type=res.stage_type,
            verdict=res.verdict,
            confidence=res.confidence,
            signals=res.signals.model_dump(),
            raw_payload=res.raw_payload,
            source_freshness=res.source_freshness.model_dump() if res.source_freshness else None,
            model_name=res.model_name,
            prompt_version=res.prompt_version,
            snapshot_at=res.snapshot_at,
            executed_at=datetime.now(UTC),
        )
        db.add(stage_analysis)
        # Commit stages individually so we don't lose them if LLM summary fails later
        await db.commit()
        await db.refresh(stage_analysis)
        stage_outputs_map[stage_analysis.id] = res

    # 5. Build summary with app.analysis.debate.build_summary(stage_outputs)
    summary_output, link_specs = await build_summary(stage_outputs_map)

    # 6. Atomic block for Summary + Links + Dual-write
    try:
        summary = ResearchSummary(
            session_id=session_id,
            decision=summary_output.decision,
            confidence=summary_output.confidence,
            bull_arguments=[arg.model_dump() for arg in summary_output.bull_arguments],
            bear_arguments=[arg.model_dump() for arg in summary_output.bear_arguments],
            price_analysis=summary_output.price_analysis.model_dump() if summary_output.price_analysis else None,
            reasons=summary_output.reasons,
            detailed_text=summary_output.detailed_text,
            warnings=summary_output.warnings,
            model_name=summary_output.model_name,
            prompt_version=summary_output.prompt_version,
            raw_payload=summary_output.raw_payload,
            token_input=summary_output.token_input,
            token_output=summary_output.token_output,
            executed_at=datetime.now(UTC),
        )
        db.add(summary)
        await db.flush()

        for spec in link_specs:
            link = SummaryStageLink(
                summary_id=summary.id,
                stage_analysis_id=spec.stage_analysis_id,
                weight=spec.weight,
                direction=spec.direction,
                rationale=spec.rationale,
            )
            db.add(link)

        # 7. If RESEARCH_PIPELINE_DUAL_WRITE_ENABLED, call adapter
        if settings.RESEARCH_PIPELINE_DUAL_WRITE_ENABLED:
            adapter = LegacyStockAnalysisAdapter()
            await adapter.write(
                db=db,
                summary=summary_output,
                summary_id=summary.id,
                stock_info_id=stock_info.id,
            )

        await db.commit()
    except Exception as e:
        logger.error(f"Failed to commit summary atomic block: {e}")
        await db.rollback()
        raise

    # 8. Update ResearchSession.status='finalized', set finalized_at
    # This is the final step, committed separately.
    session.status = "finalized"
    session.finalized_at = datetime.now(UTC)

    try:
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to commit finalized status: {e}")
        # Revert status in memory to reflect that it wasn't persisted
        session.status = "open"
        session.finalized_at = None
        raise

    return session_id
