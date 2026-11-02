"""ROB-1440 — the only writer of the intent tables.

Each method is one short transaction and no session stays open across a broker
call. :meth:`SqlAlchemyGatewayStore.reserve` holds the intent row lock while it
decides, so concurrent children of one intent cannot both spend the same cap.

ROB-1461 adds the journal reads. Each runs in a ``READ ONLY`` transaction, so a
query cannot write even by accident.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.schemas.execution_intent import ExecutionIntentV1, contract_digest
from app.services.execution_intent_gateway.decisions import (
    Child,
    ChildSnapshot,
    ChildState,
    Decision,
    IntentSnapshot,
    Refusal,
)

#: An authorization that outlives a trading day is a standing permission.
MAX_INTENT_LIFETIME: Final = timedelta(hours=24)

Decide = Callable[[IntentSnapshot, Sequence[ChildSnapshot]], Decision]


class IntentNotStorable(ValueError):
    """The intent is already expired or its authorization window is too long."""


class IntentConflict(ValueError):
    """The intent id is already stored with different contents."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """What reserving a child produced.

    A forward needs ``is_new`` with an allowing ``decision``. ``intent`` and
    ``decision`` are set only for a new row.
    """

    intent: IntentSnapshot | None = None
    child: ChildSnapshot | None = None
    is_new: bool = False
    decision: Decision | None = None
    conflict: bool = False
    refusal_without_row: Refusal | None = None


@dataclass(frozen=True, slots=True)
class IntentJournal:
    """A stored intent and all its children, in the order they were journaled."""

    intent: IntentSnapshot
    children: tuple[ChildSnapshot, ...]


class GatewayReader(Protocol):
    async def read_intent_journal(self, intent_id: UUID) -> IntentJournal | None: ...

    async def read_child(
        self, idempotency_key: str
    ) -> tuple[UUID, ChildSnapshot] | None: ...


class GatewayStore(Protocol):
    async def reserve(
        self, request: Child, *, request_digest: str, decide: Decide, now: datetime
    ) -> Reservation: ...

    async def finish(
        self,
        child_id: int,
        *,
        state: ChildState,
        broker_order_id: str | None,
        result: dict[str, Any],
        now: datetime,
    ) -> bool: ...


def _intent_snapshot(row: ExecutionIntent) -> IntentSnapshot:
    return IntentSnapshot(
        intent_id=row.id,
        strategy=row.strategy,
        account=row.account,
        venue=row.venue,
        symbol=row.symbol,
        side=row.side,
        max_quantity=row.max_quantity,
        max_notional_krw=row.max_notional_krw,
        limit_price_krw=row.limit_price_krw,
        max_slippage_bps=row.max_slippage_bps,
        position_at_authorization=row.position_at_authorization,
        min_position_after=row.min_position_after,
        max_position_after=row.max_position_after,
        expires_at=row.expires_at,
    )


def _child_snapshot(row: ExecutionChildRequest) -> ChildSnapshot:
    return ChildSnapshot(
        child_id=row.id,
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
        action=row.action,
        target_idempotency_key=row.target_idempotency_key,
        quantity=row.quantity,
        limit_price_krw=row.limit_price_krw,
        state=ChildState(row.state),
        refusal_reason=row.refusal_reason,
        broker_order_id=row.broker_order_id,
        result=row.result,
    )


def _replay_or_conflict(existing: ChildSnapshot, request_digest: str) -> Reservation:
    if existing.request_digest != request_digest:
        return Reservation(conflict=True)
    return Reservation(child=existing, is_new=False)


class SqlAlchemyGatewayStore:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def store_intent(self, intent: ExecutionIntentV1, *, now: datetime) -> None:
        """Insert an authorized intent. Same contents again is a no-op.

        This is Python's side of the contract: no child request and no executor
        path reaches it.
        """
        if intent.expires_at <= now or intent.expires_at - now > MAX_INTENT_LIFETIME:
            raise IntentNotStorable("intent expiry is outside the allowed window")
        digest = contract_digest(intent)
        inventory = intent.inventory
        async with self._session_factory() as session:
            await session.execute(
                insert(ExecutionIntent)
                .values(
                    id=intent.intent_id,
                    schema_version=intent.schema_version,
                    strategy=intent.strategy,
                    account=intent.account,
                    venue=intent.venue,
                    symbol=intent.symbol,
                    side=intent.side,
                    max_quantity=intent.max_quantity,
                    max_notional_krw=intent.max_notional_krw,
                    limit_price_krw=intent.limit_price_krw,
                    max_slippage_bps=intent.max_slippage_bps,
                    position_at_authorization=inventory.position_at_authorization,
                    min_position_after=inventory.min_position_after,
                    max_position_after=inventory.max_position_after,
                    policy_version=intent.policy_version,
                    model_version=intent.model_version,
                    authorization_ref=intent.authorization_ref,
                    expires_at=intent.expires_at,
                    intent_digest=digest,
                )
                .on_conflict_do_nothing(index_elements=[ExecutionIntent.id])
            )
            stored = await session.scalar(
                select(ExecutionIntent.intent_digest).where(
                    ExecutionIntent.id == intent.intent_id
                )
            )
            await session.commit()
        if stored != digest:
            raise IntentConflict("intent id is stored with different contents")

    async def _child_by_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> ChildSnapshot | None:
        row = await session.scalar(
            select(ExecutionChildRequest).where(
                ExecutionChildRequest.idempotency_key == idempotency_key
            )
        )
        return None if row is None else _child_snapshot(row)

    async def reserve(
        self, request: Child, *, request_digest: str, decide: Decide, now: datetime
    ) -> Reservation:
        key = request.idempotency_key
        async with self._session_factory() as session:
            intent_row = await session.scalar(
                select(ExecutionIntent)
                .where(ExecutionIntent.id == request.intent_id)
                .with_for_update()
            )
            if intent_row is None:
                existing = await self._child_by_key(session, key)
                await session.rollback()
                if existing is not None:
                    return _replay_or_conflict(existing, request_digest)
                return Reservation(refusal_without_row=Refusal.INTENT_NOT_FOUND)

            intent = _intent_snapshot(intent_row)
            existing = await self._child_by_key(session, key)
            if existing is not None:
                await session.rollback()
                return _replay_or_conflict(existing, request_digest)

            rows = await session.scalars(
                select(ExecutionChildRequest)
                .where(ExecutionChildRequest.intent_id == request.intent_id)
                .order_by(ExecutionChildRequest.id)
            )
            decision = decide(intent, [_child_snapshot(row) for row in rows])
            refused = decision.refusal is not None
            row = ExecutionChildRequest(
                intent_id=request.intent_id,
                idempotency_key=key,
                request_digest=request_digest,
                action=request.action,
                target_idempotency_key=getattr(request, "target_idempotency_key", None),
                quantity=getattr(request, "quantity", None),
                limit_price_krw=getattr(request, "limit_price_krw", None),
                state=(ChildState.REFUSED if refused else ChildState.SENDING).value,
                refusal_reason=decision.refusal.value if refused else None,
                finished_at=now if refused else None,
            )
            session.add(row)
            try:
                await session.flush()
                reserved = _child_snapshot(row)
                await session.commit()
            except IntegrityError:
                # The key was taken under a different intent, whose row lock this
                # transaction does not hold.
                await session.rollback()
                existing = await self._child_by_key(session, key)
                await session.rollback()
                if existing is None:
                    raise
                return _replay_or_conflict(existing, request_digest)
            return Reservation(
                intent=intent, child=reserved, is_new=True, decision=decision
            )

    async def read_intent_journal(self, intent_id: UUID) -> IntentJournal | None:
        """The recorded state of one intent. Takes no lock and cannot write."""
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            intent_row = await session.scalar(
                select(ExecutionIntent).where(ExecutionIntent.id == intent_id)
            )
            if intent_row is None:
                await session.rollback()
                return None
            rows = await session.scalars(
                select(ExecutionChildRequest)
                .where(ExecutionChildRequest.intent_id == intent_id)
                .order_by(ExecutionChildRequest.id)
            )
            journal = IntentJournal(
                intent=_intent_snapshot(intent_row),
                children=tuple(_child_snapshot(row) for row in rows),
            )
            await session.rollback()
            return journal

    async def read_child(
        self, idempotency_key: str
    ) -> tuple[UUID, ChildSnapshot] | None:
        """One recorded child by its key. Takes no lock and cannot write."""
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            row = await session.scalar(
                select(ExecutionChildRequest).where(
                    ExecutionChildRequest.idempotency_key == idempotency_key
                )
            )
            found = None if row is None else (row.intent_id, _child_snapshot(row))
            await session.rollback()
            return found

    async def finish(
        self,
        child_id: int,
        *,
        state: ChildState,
        broker_order_id: str | None,
        result: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Record the outcome once; a row that left ``sending`` is never rewritten."""
        async with self._session_factory() as session:
            outcome = await session.execute(
                update(ExecutionChildRequest)
                .where(
                    ExecutionChildRequest.id == child_id,
                    ExecutionChildRequest.state == ChildState.SENDING.value,
                )
                .values(
                    state=state.value,
                    broker_order_id=broker_order_id,
                    result=result,
                    finished_at=now,
                )
            )
            await session.commit()
        return (outcome.rowcount or 0) == 1
