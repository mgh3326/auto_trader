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
    ) -> tuple[list[KISLiveOrderLedger], dict[str, int]]:
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
        candidates: list[KISLiveOrderLedger] = []
        anomalies: dict[str, int] = {}
        for row in rows:
            accepted, reason = await self._terminal_projection_match(row)
            if accepted:
                candidates.append(row)
            elif reason is not None:
                anomalies[reason] = anomalies.get(reason, 0) + 1
        for row in candidates:
            self._db.expunge(row)
        return candidates, anomalies

    async def _terminal_projection_match(
        self, row: KISLiveOrderLedger
    ) -> tuple[bool, str | None]:
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
        idempotency_match = (
            OrderProposalRung.idempotency_key == row.idempotency_key
            if row.idempotency_key is not None
            else literal(False)
        )
        if (
            row.order_no is None
            and row.correlation_id is None
            and row.idempotency_key is None
        ):
            return False, None
        stmt = (
            select(
                OrderProposalRung.id,
                OrderProposalRung.state,
                broker_match.label("broker_match"),
                correlation_match.label("correlation_match"),
                idempotency_match.label("idempotency_match"),
            )
            .join(OrderProposal, OrderProposalRung.proposal_pk == OrderProposal.id)
            .where(
                or_(broker_match, correlation_match, idempotency_match),
                OrderProposal.account_mode == "kis_live",
                OrderProposal.symbol == row.symbol,
                OrderProposal.market == row.instrument_type,
            )
        )
        matches = list((await self._db.execute(stmt)).all())
        broker_ids = {match.id for match in matches if match.broker_match}
        correlation_ids = {match.id for match in matches if match.correlation_match}
        idempotency_ids = {match.id for match in matches if match.idempotency_match}
        evidence_sets = [
            ids for ids in (broker_ids, correlation_ids, idempotency_ids) if ids
        ]
        if not evidence_sets:
            return False, None
        intersection = set.intersection(*evidence_sets)
        if not intersection:
            return False, "proposal_evidence_conflict"
        if len(broker_ids) > 1:
            return False, "broker_id_duplicate"
        if not broker_ids and not idempotency_ids and len(correlation_ids) > 1:
            return False, "content_hash_only_ambiguous"
        if len(intersection) > 1:
            return False, "proposal_evidence_ambiguous"
        rung_id = next(iter(intersection))
        return next(match.state for match in matches if match.id == rung_id) in (
            _PROPOSAL_EVIDENCE_ACCEPTING_STATES
        ), None
