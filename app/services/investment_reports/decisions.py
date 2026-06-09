"""ROB-265 — Operator decision recording service.

Records one decision per (item, actor, verb) idempotently and transitions
the owning item's status when appropriate. ``skip`` is intentionally an
audit-only verb that does not move the item; everything else maps to a
new ``item.status``.

Multiple decisions per item are allowed by design — e.g. ``defer`` →
later ``approve`` produces two rows and the latest-decision projection
is the caller's responsibility (query service).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItemDecision
from app.schemas.investment_reports import (
    DecisionVerbLiteral,
    RecordDecisionRequest,
)
from app.services.investment_reports.repository import InvestmentReportsRepository

# Decision verb → resulting item.status. ``skip`` is audit-only and
# leaves the item unchanged. ROB-455 order-lifecycle verbs reuse existing
# item.status values (no new status added): ``cancel`` projects to 'denied'
# (the item won't proceed), ``reprice`` projects to 'approved' (an approval
# with adjusted levels carried in approved_payload_snapshot). The precise verb
# is preserved first-class in the decision audit row.
_ITEM_STATUS_BY_DECISION: dict[DecisionVerbLiteral, str | None] = {
    "approve": "approved",
    "deny": "denied",
    "defer": "deferred",
    "skip": None,
    "partial_approve": "approved",
    "cancel": "denied",
    "reprice": "approved",
}


class InvestmentReportDecisionService:
    """Idempotent decision recording + item status transition."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def record(
        self, request: RecordDecisionRequest
    ) -> InvestmentReportItemDecision:
        item = await self._repo.get_item_by_uuid(request.item_uuid)
        if item is None:
            raise ValueError(f"item not found: {request.item_uuid}")

        idempotency_key = request.idempotency_key or self._auto_idempotency_key(request)

        existing = await self._repo.get_decision_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.item_id != item.id:
                # Caller-supplied idempotency_key collided with a decision
                # on a different item. Returning ``existing`` would silently
                # bind a fresh request to the wrong item — reject loudly.
                raise ValueError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    f"different item (existing item_id={existing.item_id}, "
                    f"requested item_id={item.id})"
                )
            return existing

        decision = await self._repo.insert_decision(
            item_id=item.id,
            idempotency_key=idempotency_key,
            decision=request.decision,
            actor=request.actor,
            decision_note=request.decision_note,
            approved_payload_snapshot=request.approved_payload_snapshot,
        )

        new_status = _ITEM_STATUS_BY_DECISION[request.decision]
        if new_status is not None:
            await self._repo.update_item_status(item.id, new_status)

        await self._session.flush()
        return decision

    @staticmethod
    def _auto_idempotency_key(request: RecordDecisionRequest) -> str:
        # One default decision per (item, verb, actor). Caller can override
        # to allow the same operator to revisit the same verb (e.g. an
        # approval correction with a new dedup key).
        return ":".join(
            [
                "decision",
                str(request.item_uuid),
                request.decision,
                request.actor,
            ]
        )
