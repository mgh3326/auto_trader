"""The only write service for ``review.nh_mock_order_ledger``."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import NHMockOrderLedger


class NHMockOrderLedgerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record_send(
        self,
        *,
        client_order_id: str,
        broker_order_id: str | None,
        correlation_id: str,
        counterfactual_of: uuid.UUID,
        strategy: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        response_code: str | None,
        raw_response: dict[str, Any],
    ) -> NHMockOrderLedger:
        existing = (
            await self._db.execute(
                select(NHMockOrderLedger).where(
                    NHMockOrderLedger.client_order_id == client_order_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.broker_order_id == broker_order_id:
                return existing
            raise ValueError(
                "NH mock client_order_id replay conflicts with broker order id"
            )
        row = NHMockOrderLedger(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            counterfactual_of=counterfactual_of,
            strategy=strategy,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status="accepted",
            response_code=response_code,
            raw_response=raw_response,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def reconcile_fill_evidence(
        self, *, broker_order_id: str, filled_quantity: Decimal
    ) -> bool:
        """Mark a fill only after an exact order-id keyed broker evidence match."""

        if filled_quantity <= 0:
            return False
        row = (
            await self._db.execute(
                select(NHMockOrderLedger).where(
                    NHMockOrderLedger.broker_order_id == broker_order_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.filled_quantity = filled_quantity
        row.status = "filled" if filled_quantity >= row.quantity else "partial"
        from datetime import UTC, datetime

        row.reconciled_at = datetime.now(UTC)
        await self._db.commit()
        return True
