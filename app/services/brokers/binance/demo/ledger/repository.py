"""ROB-298 — Internal repository for BinanceDemoOrderLedger.

Service-internal. Never import this from outside
``app/services/brokers/binance/demo/ledger/``. The AST guard in
``tests/services/brokers/binance/demo/test_ledger_service.py``
will fail if you do.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger


class BinanceDemoLedgerRepository:
    """Direct DB surface for the demo order ledger. Service-internal."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_planned(
        self,
        *,
        instrument_id: int,
        product: str,
        venue_host: str,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        parent_client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        now: dt.datetime,
    ) -> BinanceDemoOrderLedger:
        row = BinanceDemoOrderLedger(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            parent_client_order_id=parent_client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            lifecycle_state="planned",
            planned_at=now,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceDemoOrderLedger | None:
        stmt = select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == client_order_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_state(
        self,
        row: BinanceDemoOrderLedger,
        *,
        new_state: str,
        now: dt.datetime,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        row.lifecycle_state = new_state
        row.updated_at = now
        if broker_order_id is not None:
            row.broker_order_id = broker_order_id
        if anomaly_reason is not None:
            row.anomaly_reason = anomaly_reason
            row.anomaly_at = now
        if new_state == "previewed":
            row.previewed_at = now
        elif new_state == "validated":
            row.validated_at = now
        elif new_state == "submitted":
            row.submitted_at = now
        elif new_state == "filled":
            row.filled_at = now
        elif new_state == "closed":
            row.closed_at = now
        elif new_state == "cancelled":
            row.cancelled_at = now
        elif new_state == "reconciled":
            row.reconciled_at = now
            row.last_reconciled_at = now
        if extra_metadata_merge:
            merged = dict(row.extra_metadata or {})
            merged.update(extra_metadata_merge)
            row.extra_metadata = merged
        await self._session.flush()
        return row
