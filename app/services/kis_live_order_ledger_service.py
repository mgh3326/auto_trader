"""Terminal KIS ledger candidates for proposal projection repair."""

from __future__ import annotations

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import KISLiveOrderLedger

_PROPOSAL_EVIDENCE_ACCEPTING_STATES = (
    "acked",
    "resting",
    "partially_filled",
    "unverified",
)


class KISLiveOrderLedgerService:
    """Read-only KIS terminal candidate lookup, symmetric with Toss repair."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_terminal_projection_candidates(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[KISLiveOrderLedger]:
        evidence_match = or_(
            and_(
                KISLiveOrderLedger.correlation_id.is_not(None),
                KISLiveOrderLedger.correlation_id == OrderProposalRung.correlation_id,
            ),
            and_(
                KISLiveOrderLedger.order_no.is_not(None),
                KISLiveOrderLedger.order_no == OrderProposalRung.broker_order_id,
            ),
        )
        stmt = (
            select(KISLiveOrderLedger)
            .join(OrderProposalRung, evidence_match)
            .join(OrderProposal, OrderProposalRung.proposal_pk == OrderProposal.id)
            .where(
                KISLiveOrderLedger.status.in_(
                    ("filled", "cancelled", "expired", "rejected")
                ),
                OrderProposal.account_mode == "kis_live",
                OrderProposal.symbol == KISLiveOrderLedger.symbol,
                OrderProposal.market == KISLiveOrderLedger.instrument_type,
            )
        )
        if symbol:
            stmt = stmt.where(KISLiveOrderLedger.symbol == symbol)
        if order_id:
            stmt = stmt.where(KISLiveOrderLedger.order_no == order_id)
        stmt = stmt.order_by(KISLiveOrderLedger.id.asc()).limit(limit)
        rows = list((await self._db.execute(stmt)).unique().scalars().all())
        rows = [
            row
            for row in rows
            if await self._has_unambiguous_terminal_projection_match(row)
        ]
        for row in rows:
            self._db.expunge(row)
        return rows

    async def _has_unambiguous_terminal_projection_match(
        self, row: KISLiveOrderLedger
    ) -> bool:
        broker_match = (
            OrderProposalRung.broker_order_id == row.order_no
            if row.order_no is not None
            else literal(False)
        )
        correlation_match = (
            OrderProposalRung.correlation_id == row.correlation_id
            if row.correlation_id is not None
            else literal(False)
        )
        if row.order_no is None and row.correlation_id is None:
            return False
        stmt = (
            select(
                OrderProposalRung.id,
                OrderProposalRung.state,
                broker_match.label("broker_match"),
                correlation_match.label("correlation_match"),
            )
            .join(OrderProposal, OrderProposalRung.proposal_pk == OrderProposal.id)
            .where(
                or_(broker_match, correlation_match),
                OrderProposal.account_mode == "kis_live",
                OrderProposal.symbol == row.symbol,
                OrderProposal.market == row.instrument_type,
            )
        )
        matches = list((await self._db.execute(stmt)).all())
        broker_ids = {match.id for match in matches if match.broker_match}
        correlation_ids = {match.id for match in matches if match.correlation_match}
        evidence_sets = [ids for ids in (broker_ids, correlation_ids) if ids]
        if not (
            bool(evidence_sets)
            and all(ids == evidence_sets[0] for ids in evidence_sets)
            and len(evidence_sets[0]) == 1
        ):
            return False
        rung_id = next(iter(evidence_sets[0]))
        return next(match.state for match in matches if match.id == rung_id) in (
            _PROPOSAL_EVIDENCE_ACCEPTING_STATES
        )
