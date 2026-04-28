"""Persistence wrapper for synthesized trading decision proposals.

The wrapper composes existing Trading Decision Workspace helpers and creates only
session/proposal rows. It never creates actions, outcomes, orders, watches, or
broker-side effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.trading_decision_synthesis import SynthesizedProposal
from app.services.trading_decision_service import (
    ProposalCreate,
    add_decision_proposals,
    create_decision_session,
)
from app.services.trading_decision_synthesis import build_session_synthesis_meta


def synthesized_to_proposal_create(item: SynthesizedProposal) -> ProposalCreate:
    candidate = item.candidate
    return {
        "symbol": candidate.symbol,
        "instrument_type": candidate.instrument_type,
        "proposal_kind": item.final_proposal_kind,
        "side": item.final_side,
        "original_quantity": candidate.quantity,
        "original_quantity_pct": candidate.quantity_pct,
        "original_amount": candidate.amount,
        "original_price": candidate.price,
        "original_trigger_price": candidate.trigger_price,
        "original_threshold_pct": candidate.threshold_pct,
        "original_currency": candidate.currency,
        "original_rationale": item.original_rationale,
        "original_payload": item.original_payload,
    }


async def create_synthesized_decision_session(
    session: AsyncSession,
    *,
    user_id: int,
    proposals: Sequence[SynthesizedProposal],
    generated_at: datetime,
    source_profile: str = "auto_trader_tradingagents_synthesis",
    strategy_name: str | None = None,
    market_scope: str | None = None,
    market_brief: dict[str, Any] | None = None,
    notes: str | None = None,
):
    if not proposals:
        raise ValueError("at least one synthesized proposal is required")
    synthesis_meta = build_session_synthesis_meta(list(proposals))
    merged_brief = {**(market_brief or {}), **synthesis_meta}
    db_session = await create_decision_session(
        session,
        user_id=user_id,
        source_profile=source_profile,
        strategy_name=strategy_name,
        market_scope=market_scope,
        market_brief=merged_brief,
        generated_at=generated_at,
        notes=notes,
    )
    db_proposals = await add_decision_proposals(
        session,
        session_id=db_session.id,
        proposals=[synthesized_to_proposal_create(item) for item in proposals],
    )
    return db_session, db_proposals
