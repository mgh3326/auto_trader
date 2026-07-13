# app/services/order_send_intent_service.py
"""ROB-653 P6-B — KIS pre-send reservation service.

Writes the sole double-send guard for KIS live orders (no broker idempotency
key). All writes and explicit reservation reconciliation go through this
service — no raw SQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import OrderSendIntent

logger = logging.getLogger(__name__)

# ROB-843 P1: write-ahead reservation scope for automated KIS mock scalping
# order legs. A reservation is inserted BEFORE the broker POST (proving the DB is
# writable) and released only when the order is confirmed fully tracked or
# proven not sent. An UNRESOLVED reservation is a durable "in-flight / uncertain"
# marker that survives restart and fail-closes new orders until reconciliation.
KIS_MOCK_SCALPING_SCOPE = "kis_mock_scalping"


class DuplicateOrderIntent(Exception):
    """Raised when (account_scope, idempotency_key) is already reserved."""


@dataclass(frozen=True)
class OrderSendIntentReservation:
    row_id: int
    idempotency_key: str
    side: str | None


class OrderSendIntentService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
        conflicting_key_sides: tuple[tuple[str, str], ...] = (),
    ) -> int:
        if conflicting_key_sides:
            predicates = [
                and_(
                    OrderSendIntent.idempotency_key == key,
                    or_(
                        OrderSendIntent.side.is_(None),
                        func.lower(func.trim(OrderSendIntent.side))
                        == conflicting_side.strip().lower(),
                    ),
                )
                for key, conflicting_side in conflicting_key_sides
            ]
            existing = await self._db.scalar(
                select(OrderSendIntent.id)
                .where(
                    OrderSendIntent.account_scope == account_scope,
                    or_(*predicates),
                )
                .with_for_update()
                .limit(1)
            )
            if existing is not None:
                raise DuplicateOrderIntent(
                    f"conflicting order intent already reserved: {account_scope}"
                )

        row = OrderSendIntent(
            account_scope=account_scope,
            idempotency_key=idempotency_key,
            symbol=symbol,
            side=side,
        )
        self._db.add(row)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise DuplicateOrderIntent(
                f"order intent already reserved: {account_scope}/{idempotency_key}"
            ) from exc
        rid = row.id
        await self._db.commit()
        return rid

    async def release(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
    ) -> int:
        result = await self._db.execute(
            delete(OrderSendIntent).where(
                OrderSendIntent.account_scope == account_scope,
                OrderSendIntent.idempotency_key == idempotency_key,
            )
        )
        await self._db.commit()
        return int(result.rowcount or 0)

    async def release_if_matches(
        self,
        *,
        account_scope: str,
        row_id: int,
        idempotency_key: str,
        side: str | None,
    ) -> int:
        """Delete only the exact reservation row observed before external I/O."""
        side_predicate = (
            OrderSendIntent.side.is_(None)
            if side is None
            else OrderSendIntent.side == side
        )
        result = await self._db.execute(
            delete(OrderSendIntent).where(
                OrderSendIntent.id == row_id,
                OrderSendIntent.account_scope == account_scope,
                OrderSendIntent.idempotency_key == idempotency_key,
                side_predicate,
            )
        )
        await self._db.commit()
        return int(result.rowcount or 0)

    async def has_reservations(self, *, account_scope: str) -> bool:
        """True if ANY unresolved reservation exists in ``account_scope``.

        Used as the durable fail-close signal (ROB-843 P1): while an automated
        mock order is in-flight/uncertain its reservation is still present, so
        new orders must fail-close until reconciliation releases it.
        """
        found = await self._db.scalar(
            select(func.count())
            .select_from(OrderSendIntent)
            .where(OrderSendIntent.account_scope == account_scope)
        )
        return bool(found or 0)

    async def list_keys(self, *, account_scope: str) -> list[str]:
        """The idempotency keys of all unresolved reservations in ``account_scope``."""
        rows = await self._db.execute(
            select(OrderSendIntent.idempotency_key).where(
                OrderSendIntent.account_scope == account_scope
            )
        )
        return [k for (k,) in rows.all()]

    async def list_reservations(
        self, *, account_scope: str
    ) -> list[OrderSendIntentReservation]:
        """Immutable row references used across external reconciliation I/O."""
        rows = await self._db.execute(
            select(
                OrderSendIntent.id,
                OrderSendIntent.idempotency_key,
                OrderSendIntent.side,
            ).where(OrderSendIntent.account_scope == account_scope)
        )
        return [
            OrderSendIntentReservation(
                row_id=row_id,
                idempotency_key=idempotency_key,
                side=side,
            )
            for row_id, idempotency_key, side in rows.all()
        ]
