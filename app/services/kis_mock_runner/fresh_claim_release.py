"""Release a KIS claim only while its position observation remains fresh."""

from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import aliased

from app.models.review import OrderSendIntent
from app.services.mock_integration.coordination import (
    DurableClaim,
    DurableSendClaimAdapter,
    TerminalClaimEvidence,
)
from app.services.order_send_intent_service import (
    KIS_MOCK_SCALPING_SCOPE,
    OrderSendIntentService,
)


class _FreshKISIntentService(OrderSendIntentService):
    def __init__(self, db, *, observed_at: datetime, max_age: timedelta) -> None:
        super().__init__(db)
        self._observed_at = observed_at
        self._deadline = observed_at + max_age

    async def release_if_matches(
        self, *, account_scope: str, row_id: int, idempotency_key: str, side: str | None
    ) -> int:
        side_predicate = (
            OrderSendIntent.side.is_(None)
            if side is None
            else OrderSendIntent.side == side
        )
        identity = (
            OrderSendIntent.id == row_id,
            OrderSendIntent.account_scope == account_scope,
            OrderSendIntent.idempotency_key == idempotency_key,
            side_predicate,
        )
        # Wait for an existing row lock first; the clock predicate must run
        # after that wait, not when PostgreSQL initially discovers the row.
        await self._db.scalar(
            select(OrderSendIntent.id).where(*identity).with_for_update()
        )
        other = aliased(OrderSendIntent)
        other_physical = exists(
            select(other.id).where(
                other.account_scope == account_scope, other.id != row_id
            )
        )
        legacy = exists(
            select(other.id).where(other.account_scope == KIS_MOCK_SCALPING_SCOPE)
        )
        result = await self._db.execute(
            delete(OrderSendIntent).where(
                *identity,
                ~other_physical,
                ~legacy,
                func.clock_timestamp() >= self._observed_at,
                func.clock_timestamp() <= self._deadline,
            )
        )
        return int(result.rowcount or 0)


async def release_fresh_claim(
    claims: DurableSendClaimAdapter,
    claim: DurableClaim,
    evidence: TerminalClaimEvidence,
    *,
    observed_at: datetime,
    max_age: timedelta,
) -> int:
    # Keep the shared terminal gate and the caller's transaction, but perform
    # the actual delete with the KIS-specific DB-time freshness condition.
    if (
        type(claims) is not DurableSendClaimAdapter
        or type(claims._intents) is not OrderSendIntentService
    ):
        raise ValueError("kis_fresh_claim_service_required")
    service = _FreshKISIntentService(
        claims._intents._db, observed_at=observed_at, max_age=max_age
    )
    return await DurableSendClaimAdapter(service).release_with_terminal_evidence(
        claim, evidence
    )
