from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ActionKind,
    OutcomeHorizon,
    ProposalKind,
    TrackKind,
    TradingDecisionAction,
    TradingDecisionCounterfactual,
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
    UserResponse,
)


class ProposalCreate(TypedDict, total=False):
    symbol: str
    instrument_type: InstrumentType
    proposal_kind: ProposalKind
    side: str  # 'buy', 'sell', 'none'
    original_quantity: Decimal | None
    original_quantity_pct: Decimal | None
    original_amount: Decimal | None
    original_price: Decimal | None
    original_trigger_price: Decimal | None
    original_threshold_pct: Decimal | None
    original_currency: str | None
    original_rationale: str | None
    original_payload: dict


async def create_decision_session(
    session: AsyncSession,
    *,
    user_id: int,
    source_profile: str,
    strategy_name: str | None = None,
    market_scope: str | None = None,
    market_brief: dict | None = None,
    generated_at: datetime,
    notes: str | None = None,
) -> TradingDecisionSession:
    """Create a new trading decision session."""
    db_session = TradingDecisionSession(
        user_id=user_id,
        source_profile=source_profile,
        strategy_name=strategy_name,
        market_scope=market_scope,
        market_brief=market_brief,
        generated_at=generated_at,
        notes=notes,
    )
    session.add(db_session)
    await session.flush()
    await session.refresh(db_session)
    return db_session


async def add_decision_proposals(
    session: AsyncSession,
    *,
    session_id: int,
    proposals: Sequence[ProposalCreate],
) -> list[TradingDecisionProposal]:
    """Add multiple proposals to a session."""
    db_proposals = []
    for p in proposals:
        db_p = TradingDecisionProposal(
            session_id=session_id,
            symbol=p["symbol"],
            instrument_type=p["instrument_type"],
            proposal_kind=p["proposal_kind"],
            side=p.get("side", "none"),
            original_quantity=p.get("original_quantity"),
            original_quantity_pct=p.get("original_quantity_pct"),
            original_amount=p.get("original_amount"),
            original_price=p.get("original_price"),
            original_trigger_price=p.get("original_trigger_price"),
            original_threshold_pct=p.get("original_threshold_pct"),
            original_currency=p.get("original_currency"),
            original_rationale=p.get("original_rationale"),
            original_payload=p["original_payload"],
        )
        db_proposals.append(db_p)

    session.add_all(db_proposals)
    await session.flush()
    for db_p in db_proposals:
        await session.refresh(db_p)
    return db_proposals


async def record_user_response(
    session: AsyncSession,
    *,
    proposal_id: int,
    response: UserResponse,
    user_quantity: Decimal | None = None,
    user_quantity_pct: Decimal | None = None,
    user_amount: Decimal | None = None,
    user_price: Decimal | None = None,
    user_trigger_price: Decimal | None = None,
    user_threshold_pct: Decimal | None = None,
    user_note: str | None = None,
    responded_at: datetime | None = None,
) -> TradingDecisionProposal:
    """Record user's response to a proposal."""
    result = await session.execute(
        select(TradingDecisionProposal).where(TradingDecisionProposal.id == proposal_id)
    )
    db_proposal = result.scalar_one()

    db_proposal.user_response = response
    db_proposal.user_quantity = user_quantity
    db_proposal.user_quantity_pct = user_quantity_pct
    db_proposal.user_amount = user_amount
    db_proposal.user_price = user_price
    db_proposal.user_trigger_price = user_trigger_price
    db_proposal.user_threshold_pct = user_threshold_pct
    db_proposal.user_note = user_note
    db_proposal.responded_at = responded_at or datetime.now(
        db_proposal.created_at.tzinfo
    )

    await session.flush()
    await session.refresh(db_proposal)
    return db_proposal


async def record_decision_action(
    session: AsyncSession,
    *,
    proposal_id: int,
    action_kind: ActionKind,
    external_order_id: str | None = None,
    external_paper_id: str | None = None,
    external_watch_id: str | None = None,
    external_source: str | None = None,
    payload_snapshot: dict,
) -> TradingDecisionAction:
    """Record an action taken based on a proposal."""
    # Validate invariant: at least one external id unless no_action/manual_note
    if action_kind not in (ActionKind.no_action, ActionKind.manual_note):
        if not any([external_order_id, external_paper_id, external_watch_id]):
            raise ValueError(
                f"Action kind '{action_kind}' requires at least one external ID."
            )

    db_action = TradingDecisionAction(
        proposal_id=proposal_id,
        action_kind=action_kind,
        external_order_id=external_order_id,
        external_paper_id=external_paper_id,
        external_watch_id=external_watch_id,
        external_source=external_source,
        payload_snapshot=payload_snapshot,
    )
    session.add(db_action)
    await session.flush()
    await session.refresh(db_action)
    return db_action


async def create_counterfactual_track(
    session: AsyncSession,
    *,
    proposal_id: int,
    track_kind: TrackKind,
    baseline_price: Decimal,
    baseline_at: datetime,
    quantity: Decimal | None = None,
    payload: dict,
    notes: str | None = None,
) -> TradingDecisionCounterfactual:
    """Create a counterfactual track for a proposal."""
    db_track = TradingDecisionCounterfactual(
        proposal_id=proposal_id,
        track_kind=track_kind,
        baseline_price=baseline_price,
        baseline_at=baseline_at,
        quantity=quantity,
        payload=payload,
        notes=notes,
    )
    session.add(db_track)
    await session.flush()
    await session.refresh(db_track)
    return db_track


async def record_outcome_mark(
    session: AsyncSession,
    *,
    proposal_id: int,
    track_kind: TrackKind,
    horizon: OutcomeHorizon,
    price_at_mark: Decimal,
    counterfactual_id: int | None = None,
    pnl_pct: Decimal | None = None,
    pnl_amount: Decimal | None = None,
    marked_at: datetime,
    payload: dict | None = None,
) -> TradingDecisionOutcome:
    """Record an outcome mark for a track."""
    # Validate invariant: counterfactual_id IS NULL <=> track_kind == 'accepted_live'
    if track_kind == TrackKind.accepted_live:
        if counterfactual_id is not None:
            raise ValueError("accepted_live track must not have a counterfactual_id")
    else:
        if counterfactual_id is None:
            raise ValueError(f"track_kind '{track_kind}' requires a counterfactual_id")

    db_outcome = TradingDecisionOutcome(
        proposal_id=proposal_id,
        counterfactual_id=counterfactual_id,
        track_kind=track_kind,
        horizon=horizon,
        price_at_mark=price_at_mark,
        pnl_pct=pnl_pct,
        pnl_amount=pnl_amount,
        marked_at=marked_at,
        payload=payload,
    )
    session.add(db_outcome)
    await session.flush()
    await session.refresh(db_outcome)
    return db_outcome


async def get_session_by_uuid(
    session: AsyncSession,
    *,
    session_uuid: UUID,
    user_id: int,
) -> TradingDecisionSession | None:
    """Return session iff it exists AND belongs to user_id; eager-load proposals."""
    result = await session.execute(
        select(TradingDecisionSession)
        .where(
            TradingDecisionSession.session_uuid == session_uuid,
            TradingDecisionSession.user_id == user_id,
        )
        .options(
            selectinload(TradingDecisionSession.proposals).selectinload(
                TradingDecisionProposal.actions
            ),
            selectinload(TradingDecisionSession.proposals).selectinload(
                TradingDecisionProposal.counterfactuals
            ),
            selectinload(TradingDecisionSession.proposals).selectinload(
                TradingDecisionProposal.outcomes
            ),
        )
    )
    return result.scalar_one_or_none()


async def list_user_sessions(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> tuple[list[tuple[TradingDecisionSession, int, int]], int]:
    """
    Return (rows, total). Each row is (session, proposals_count, pending_count).
    Single SQL query with grouped counts to avoid N+1.
    """
    # Build base filters
    base_filters = [TradingDecisionSession.user_id == user_id]
    if status:
        base_filters.append(TradingDecisionSession.status == status)

    # Get total count
    total_result = await session.execute(
        select(func.count(TradingDecisionSession.id)).where(*base_filters)
    )
    total = total_result.scalar() or 0

    # Get sessions with counts
    stmt = (
        select(
            TradingDecisionSession,
            func.count(TradingDecisionProposal.id).label("proposals_count"),
            func.count(TradingDecisionProposal.id)
            .filter(TradingDecisionProposal.user_response == "pending")
            .label("pending_count"),
        )
        .outerjoin(
            TradingDecisionProposal,
            TradingDecisionProposal.session_id == TradingDecisionSession.id,
        )
        .where(*base_filters)
        .group_by(TradingDecisionSession.id)
        .order_by(TradingDecisionSession.generated_at.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await session.execute(stmt)
    rows = [
        (row.TradingDecisionSession, row.proposals_count, row.pending_count)
        for row in result.all()
    ]

    return rows, total


async def get_proposal_by_uuid(
    session: AsyncSession,
    *,
    proposal_uuid: UUID,
    user_id: int,
) -> TradingDecisionProposal | None:
    """JOIN sessions to enforce ownership. Eager-load actions/counterfactuals/outcomes."""
    result = await session.execute(
        select(TradingDecisionProposal)
        .join(TradingDecisionSession)
        .where(
            TradingDecisionProposal.proposal_uuid == proposal_uuid,
            TradingDecisionSession.user_id == user_id,
        )
        .options(
            selectinload(TradingDecisionProposal.session),
            selectinload(TradingDecisionProposal.actions),
            selectinload(TradingDecisionProposal.counterfactuals),
            selectinload(TradingDecisionProposal.outcomes),
        )
    )
    return result.scalar_one_or_none()
