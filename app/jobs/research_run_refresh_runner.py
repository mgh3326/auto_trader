"""ROB-26 — Read-only research-run refresh orchestrator.

Wraps the ROB-25 live-refresh / decision-session pipeline with deterministic,
read-only summary semantics. Never raises on operational skip conditions;
always returns a structured dict summary.

Scheduler-agnostic: no Taskiq imports here, so this function can be wrapped
by any scheduler (Taskiq cron tasks in app/tasks/research_run_refresh_tasks.py,
or a future Prefect @flow in a separate package).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncContextManager, Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import now_kst
from app.schemas.research_run_decision_session import (
    ResearchRunDecisionSessionRequest,
    ResearchRunSelector,
)
from app.services import (
    research_run_decision_session_service,
    research_run_live_refresh_service,
)

logger = logging.getLogger(__name__)

StageLiteral = Literal["preopen", "nxt_aftermarket"]
MarketScopeLiteral = Literal["kr"]
StatusLiteral = Literal["completed", "disabled", "skipped", "error"]


class ResearchRunRefreshSummary(TypedDict, total=False):
    status: StatusLiteral
    reason: str
    stage: str
    market_scope: str
    research_run_uuid: str | None
    session_uuid: str | None
    proposal_count: int
    reconciliation_count: int
    refreshed_at: str | None
    warnings: list[str]


_KR_PREOPEN_WINDOW = ((8, 0), (9, 30))
_KR_NXT_WINDOW = ((15, 30), (20, 30))


def _within_window(*, stage: StageLiteral, now: datetime) -> bool:
    """Return True if `now` falls within the allowed trading window for `stage`."""
    weekday = now.weekday()  # Mon=0..Sun=6
    if weekday >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    if stage == "preopen":
        start = _KR_PREOPEN_WINDOW[0][0] * 60 + _KR_PREOPEN_WINDOW[0][1]
        end = _KR_PREOPEN_WINDOW[1][0] * 60 + _KR_PREOPEN_WINDOW[1][1]
    elif stage == "nxt_aftermarket":
        start = _KR_NXT_WINDOW[0][0] * 60 + _KR_NXT_WINDOW[0][1]
        end = _KR_NXT_WINDOW[1][0] * 60 + _KR_NXT_WINDOW[1][1]
    else:
        return False
    return start <= minutes <= end


@asynccontextmanager
async def _default_db_factory():
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


async def run_research_run_refresh(
    *,
    stage: StageLiteral,
    market_scope: MarketScopeLiteral = "kr",
    db_factory: Callable[[], AsyncContextManager[AsyncSession]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    now_local: Callable[[], datetime] = now_kst,
) -> ResearchRunRefreshSummary:
    """Read-only refresh of the latest research run for the configured operator.

    Returns a structured summary; never raises on operational skip conditions
    (disabled, no operator, outside hours, no run, empty run).
    """
    base: ResearchRunRefreshSummary = {
        "stage": stage,
        "market_scope": market_scope,
        "research_run_uuid": None,
        "session_uuid": None,
        "proposal_count": 0,
        "reconciliation_count": 0,
        "refreshed_at": None,
        "warnings": [],
    }

    if not settings.research_run_refresh_enabled:
        logger.info(
            "research_run_refresh disabled; skipping (%s/%s)", stage, market_scope
        )
        return {**base, "status": "disabled", "reason": "research_run_refresh_disabled"}

    user_id = settings.research_run_refresh_user_id
    if user_id is None:
        logger.info(
            "research_run_refresh has no operator user; skipping (%s/%s)",
            stage,
            market_scope,
        )
        return {**base, "status": "skipped", "reason": "no_operator_user_configured"}

    if settings.research_run_refresh_market_hours_only and not _within_window(
        stage=stage, now=now_local()
    ):
        logger.info(
            "research_run_refresh outside trading hours; skipping (%s/%s)",
            stage,
            market_scope,
        )
        return {**base, "status": "skipped", "reason": "outside_trading_hours"}

    _factory = db_factory or _default_db_factory
    async with _factory() as db:
        try:
            try:
                research_run = await research_run_decision_session_service.resolve_research_run(
                    db,
                    user_id=user_id,
                    selector=ResearchRunSelector(
                        market_scope=market_scope,
                        stage=stage,
                        status="open",
                    ),
                )
            except research_run_decision_session_service.ResearchRunNotFound:
                return {**base, "status": "skipped", "reason": "no_research_run"}

            snapshot = await research_run_live_refresh_service.build_live_refresh_snapshot(
                db, run=research_run
            )

            try:
                result = await research_run_decision_session_service.create_decision_session_from_research_run(
                    db,
                    user_id=user_id,
                    research_run=research_run,
                    snapshot=snapshot,
                    request=ResearchRunDecisionSessionRequest(
                        selector=ResearchRunSelector(
                            run_uuid=research_run.run_uuid,
                        ),
                        include_tradingagents=False,
                        notes=f"scheduled:{stage}",
                        generated_at=None,
                    ),
                    now=now,
                )
            except research_run_decision_session_service.EmptyResearchRunError:
                return {
                    **base,
                    "status": "skipped",
                    "reason": "empty_research_run",
                    "research_run_uuid": str(research_run.run_uuid),
                }

            await db.commit()

            return {
                **base,
                "status": "completed",
                "research_run_uuid": str(result.research_run.run_uuid),
                "session_uuid": str(result.session.session_uuid),
                "proposal_count": result.proposal_count,
                "reconciliation_count": result.reconciliation_count,
                "refreshed_at": result.refreshed_at.isoformat(),
                "warnings": list(result.warnings),
            }
        except Exception:
            await db.rollback()
            logger.exception(
                "research_run_refresh failed (stage=%s market=%s)",
                stage,
                market_scope,
            )
            raise
