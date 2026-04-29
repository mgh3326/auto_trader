"""Preopen dashboard aggregation service (ROB-39).

Read-only. Never imports broker, order, watch, intent, or credential modules.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_run import ResearchRun, ResearchRunCandidate
from app.models.trading_decision import TradingDecisionProposal, TradingDecisionSession
from app.schemas.preopen import (
    CandidateSummary,
    LinkedSessionRef,
    PreopenLatestResponse,
    ReconciliationSummary,
)
from app.services import research_run_service

logger = logging.getLogger(__name__)

_FAIL_OPEN = PreopenLatestResponse(
    has_run=False,
    advisory_used=False,
    advisory_skipped_reason="no_open_preopen_run",
    run_uuid=None,
    market_scope=None,
    stage=None,
    status=None,
    strategy_name=None,
    source_profile=None,
    generated_at=None,
    created_at=None,
    notes=None,
    market_brief=None,
    source_freshness=None,
    source_warnings=[],
    advisory_links=[],
    candidate_count=0,
    reconciliation_count=0,
    candidates=[],
    reconciliations=[],
    linked_sessions=[],
)


async def _linked_sessions(
    db: AsyncSession,
    *,
    run: ResearchRun,
    user_id: int,
) -> list[LinkedSessionRef]:
    """Best-effort: find TradingDecisionSessions created from this run."""
    run_uuid_str = str(run.run_uuid)
    try:
        stmt = (
            select(TradingDecisionSession)
            .join(
                TradingDecisionProposal,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .where(
                TradingDecisionSession.user_id == user_id,
                TradingDecisionProposal.original_payload["research_run_id"].astext
                == run_uuid_str,
            )
            .distinct()
            .order_by(TradingDecisionSession.created_at.desc())
            .limit(5)
        )
        result = await db.execute(stmt)
        sessions = result.scalars().all()
        return [
            LinkedSessionRef(
                session_uuid=s.session_uuid,
                status=s.status,
                created_at=s.created_at,
            )
            for s in sessions
        ]
    except Exception:
        # Fail-open: linked session lookup must not block the page
        logger.warning(
            "Failed to look up linked preopen decision sessions",
            exc_info=True,
            extra={"run_uuid": run_uuid_str, "user_id": user_id},
        )
        return []


def _map_candidates(run: ResearchRun) -> list[CandidateSummary]:
    def sort_key(c: ResearchRunCandidate) -> tuple:
        side_order = {"buy": 0, "sell": 1, "none": 2}
        return (side_order.get(c.side, 9), -(c.confidence or -1), c.symbol)

    return [
        CandidateSummary(
            candidate_uuid=c.candidate_uuid,
            symbol=c.symbol,
            instrument_type=c.instrument_type.value
            if hasattr(c.instrument_type, "value")
            else str(c.instrument_type),
            side=c.side,  # type: ignore[arg-type]
            candidate_kind=c.candidate_kind,
            proposed_price=c.proposed_price,
            proposed_qty=c.proposed_qty,
            confidence=c.confidence,
            rationale=c.rationale,
            currency=c.currency,
            warnings=list(c.warnings),
        )
        for c in sorted(run.candidates, key=sort_key)
    ]


def _map_reconciliations(run: ResearchRun) -> list[ReconciliationSummary]:
    return [
        ReconciliationSummary(
            order_id=r.order_id,
            symbol=r.symbol,
            market=r.market,
            side=r.side,  # type: ignore[arg-type]
            classification=r.classification,
            nxt_classification=r.nxt_classification,
            nxt_actionable=r.nxt_actionable,
            gap_pct=r.gap_pct,
            summary=r.summary,
            reasons=list(r.reasons),
            warnings=list(r.warnings),
        )
        for r in sorted(run.reconciliations, key=lambda r: (r.classification, r.symbol))
    ]


def _advisory_skipped_reason(run: ResearchRun) -> str | None:
    if not run.candidates:
        return "no_candidates"
    advisory_failure_markers = {
        "advisory_failed",
        "advisory_error",
        "advisory_timeout",
        "tradingagents_not_configured",
    }
    for w in run.source_warnings:
        if w in advisory_failure_markers:
            return w
    return None


async def get_latest_preopen_dashboard(
    db: AsyncSession,
    *,
    user_id: int,
    market_scope: str,
) -> PreopenLatestResponse:
    run = await research_run_service.get_latest_research_run(
        db,
        user_id=user_id,
        market_scope=market_scope,
        stage="preopen",
        status="open",
    )

    if run is None:
        return _FAIL_OPEN

    candidates = _map_candidates(run)
    reconciliations = _map_reconciliations(run)
    advisory_reason = _advisory_skipped_reason(run)
    linked = await _linked_sessions(db, run=run, user_id=user_id)

    return PreopenLatestResponse(
        has_run=True,
        advisory_used=bool(run.advisory_links) and advisory_reason is None,
        advisory_skipped_reason=advisory_reason,
        run_uuid=run.run_uuid,
        market_scope=run.market_scope,  # type: ignore[arg-type]
        stage="preopen",
        status=run.status,
        strategy_name=run.strategy_name,
        source_profile=run.source_profile,
        generated_at=run.generated_at,
        created_at=run.created_at,
        notes=run.notes,
        market_brief=run.market_brief,
        source_freshness=run.source_freshness,
        source_warnings=list(run.source_warnings),
        advisory_links=list(run.advisory_links),
        candidate_count=len(candidates),
        reconciliation_count=len(reconciliations),
        candidates=candidates,
        reconciliations=reconciliations,
        linked_sessions=linked,
    )
