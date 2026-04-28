"""Research Run persistence service.

Async CRUD over research_runs, research_run_candidates, and
research_run_pending_reconciliations tables. Pure persistence: consumes
already-classified DTOs from ROB-22/23, never re-classifies.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunPendingReconciliation,
)
from app.models.trading import InstrumentType

if TYPE_CHECKING:
    from app.services.nxt_classifier_service import NxtClassifierItem
    from app.services.pending_reconciliation_service import PendingReconciliationItem


class CandidateCreate(TypedDict, total=False):
    symbol: str
    instrument_type: InstrumentType
    side: str
    candidate_kind: str
    proposed_price: Decimal | None
    proposed_qty: Decimal | None
    confidence: int | None
    rationale: str | None
    currency: str | None
    source_freshness: dict[str, Any] | None
    warnings: list[str]
    payload: dict[str, Any]


class PendingReconciliationCreate(TypedDict, total=False):
    candidate_id: int | None
    order_id: str
    symbol: str
    market: str
    side: str
    classification: str
    nxt_classification: str | None
    nxt_actionable: bool | None
    gap_pct: Decimal | None
    reasons: list[str]
    warnings: list[str]
    decision_support: dict[str, Any]
    summary: str | None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _validate_advisory_links(links: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for link in links:
        if (
            link.get("advisory_only") is not True
            or link.get("execution_allowed") is not False
        ):
            raise ValueError(
                "advisory_links must be advisory-only with execution_allowed=False"
            )
        validated.append(_json_safe(dict(link)))
    return validated


async def create_research_run(
    session: AsyncSession,
    *,
    user_id: int,
    market_scope: str,
    stage: str,
    source_profile: str,
    strategy_name: str | None = None,
    notes: str | None = None,
    market_brief: dict[str, Any] | None = None,
    source_freshness: dict[str, Any] | None = None,
    source_warnings: Sequence[str] = (),
    advisory_links: Sequence[dict[str, Any]] = (),
    generated_at: datetime,
) -> ResearchRun:
    validated_links = _validate_advisory_links(advisory_links)
    run = ResearchRun(
        user_id=user_id,
        market_scope=market_scope,
        stage=stage,
        source_profile=source_profile,
        strategy_name=strategy_name,
        notes=notes,
        market_brief=_json_safe(market_brief),
        source_freshness=_json_safe(source_freshness),
        source_warnings=list(source_warnings),
        advisory_links=validated_links,
        generated_at=generated_at,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    return run


async def add_research_run_candidates(
    session: AsyncSession,
    *,
    research_run_id: int,
    candidates: Sequence[CandidateCreate],
) -> list[ResearchRunCandidate]:
    created: list[ResearchRunCandidate] = []
    for c in candidates:
        candidate = ResearchRunCandidate(
            research_run_id=research_run_id,
            symbol=c["symbol"],
            instrument_type=c["instrument_type"],
            side=c.get("side", "none"),
            candidate_kind=c["candidate_kind"],
            proposed_price=c.get("proposed_price"),
            proposed_qty=c.get("proposed_qty"),
            confidence=c.get("confidence"),
            rationale=c.get("rationale"),
            currency=c.get("currency"),
            source_freshness=_json_safe(c.get("source_freshness")),
            warnings=list(c.get("warnings", [])),
            payload=_json_safe(dict(c.get("payload", {}))),
        )
        session.add(candidate)
        created.append(candidate)
    await session.flush()
    for c in created:
        await session.refresh(c)
    return created


async def attach_pending_reconciliations(
    session: AsyncSession,
    *,
    research_run_id: int,
    items: Sequence[PendingReconciliationCreate],
) -> list[ResearchRunPendingReconciliation]:
    created: list[ResearchRunPendingReconciliation] = []
    for item in items:
        recon = ResearchRunPendingReconciliation(
            research_run_id=research_run_id,
            candidate_id=item.get("candidate_id"),
            order_id=item["order_id"],
            symbol=item["symbol"],
            market=item["market"],
            side=item["side"],
            classification=item["classification"],
            nxt_classification=item.get("nxt_classification"),
            nxt_actionable=item.get("nxt_actionable"),
            gap_pct=item.get("gap_pct"),
            reasons=list(item.get("reasons", [])),
            warnings=list(item.get("warnings", [])),
            decision_support=_json_safe(dict(item.get("decision_support", {}))),
            summary=item.get("summary"),
        )
        session.add(recon)
        created.append(recon)
    await session.flush()
    for r in created:
        await session.refresh(r)
    return created


async def get_research_run_by_uuid(
    session: AsyncSession,
    *,
    run_uuid: UUID,
    user_id: int,
) -> ResearchRun | None:
    result = await session.execute(
        select(ResearchRun)
        .options(
            selectinload(ResearchRun.candidates),
            selectinload(ResearchRun.reconciliations),
        )
        .where(ResearchRun.run_uuid == run_uuid, ResearchRun.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_user_research_runs(
    session: AsyncSession,
    *,
    user_id: int,
    market_scope: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[ResearchRun, int, int]], int]:
    count_stmt = select(func.count(ResearchRun.id)).where(
        ResearchRun.user_id == user_id
    )
    if market_scope is not None:
        count_stmt = count_stmt.where(ResearchRun.market_scope == market_scope)
    if stage is not None:
        count_stmt = count_stmt.where(ResearchRun.stage == stage)
    if status is not None:
        count_stmt = count_stmt.where(ResearchRun.status == status)

    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    query = (
        select(
            ResearchRun,
            func.count(distinct(ResearchRunCandidate.id)).label("candidate_count"),
            func.count(distinct(ResearchRunPendingReconciliation.id)).label(
                "reconciliation_count"
            ),
        )
        .outerjoin(
            ResearchRunCandidate,
            ResearchRunCandidate.research_run_id == ResearchRun.id,
        )
        .outerjoin(
            ResearchRunPendingReconciliation,
            ResearchRunPendingReconciliation.research_run_id == ResearchRun.id,
        )
        .where(ResearchRun.user_id == user_id)
        .group_by(ResearchRun.id)
        .order_by(ResearchRun.generated_at.desc())
        .limit(limit)
        .offset(offset)
    )

    if market_scope is not None:
        query = query.where(ResearchRun.market_scope == market_scope)
    if stage is not None:
        query = query.where(ResearchRun.stage == stage)
    if status is not None:
        query = query.where(ResearchRun.status == status)

    result = await session.execute(query)
    rows = [
        (row.ResearchRun, row.candidate_count, row.reconciliation_count)
        for row in result.all()
    ]
    return rows, total


def reconciliation_create_from_recon(
    item: PendingReconciliationItem,
    *,
    candidate_id: int | None = None,
    summary: str | None = None,
) -> PendingReconciliationCreate:
    return {
        "candidate_id": candidate_id,
        "order_id": item.order_id,
        "symbol": item.symbol,
        "market": item.market,
        "side": item.side,
        "classification": item.classification,
        "nxt_classification": None,
        "nxt_actionable": item.nxt_actionable,
        "gap_pct": item.gap_pct,
        "reasons": list(item.reasons),
        "warnings": list(item.warnings),
        "decision_support": dict(item.decision_support),
        "summary": summary,
    }


def reconciliation_create_from_nxt(
    item: NxtClassifierItem,
    *,
    candidate_id: int | None = None,
    market: str = "kr",
) -> PendingReconciliationCreate:
    if item.kind != "pending_order":
        raise ValueError(
            "reconciliation_create_from_nxt only accepts pending_order kind; "
            "candidates and holdings are persisted via add_research_run_candidates"
        )
    return {
        "candidate_id": candidate_id,
        "order_id": item.item_id,
        "symbol": item.symbol,
        "market": market,
        "side": item.side or "buy",
        "classification": "unknown",
        "nxt_classification": item.classification,
        "nxt_actionable": item.nxt_actionable,
        "gap_pct": None,
        "reasons": list(item.reasons),
        "warnings": list(item.warnings),
        "decision_support": dict(item.decision_support),
        "summary": item.summary,
    }
