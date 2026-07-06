# app/services/order_send_intent_service.py
"""ROB-653 P6-B — KIS pre-send reservation service.

Writes the sole double-send guard for KIS live orders (no broker idempotency
key). All writes go through this service — no raw SQL. Never read by reconcile.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import OrderSendIntent

logger = logging.getLogger(__name__)


class DuplicateOrderIntent(Exception):
    """Raised when (account_scope, idempotency_key) is already reserved."""


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
    ) -> int:
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
