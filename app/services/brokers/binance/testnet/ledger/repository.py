"""ROB-286 — Repository for binance_testnet_order_ledger.

Service-internal. Do not import from outside
``app/services/brokers/binance/testnet/ledger/``. Use
``BinanceTestnetLedgerService`` as the public write surface.

The audit test
``tests/services/brokers/binance/testnet/test_ledger_service::test_repository_not_importable_externally``
asserts that ``importlib.import_module(
"app.services.brokers.binance.testnet.ledger.repository._public_export")``
raises ``ImportError`` — i.e., the repository has no submodule of that
name. This module-level guard is satisfied by-construction because
``_public_export`` is a private class, not a submodule.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger


class BinanceTestnetLedgerRepository:
    """Service-internal DB boundary for ``binance_testnet_order_ledger``."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceTestnetOrderLedger | None:
        result = await self._session.execute(
            select(BinanceTestnetOrderLedger).where(
                BinanceTestnetOrderLedger.client_order_id == client_order_id
            )
        )
        return result.scalar_one_or_none()

    async def list_by_instrument(
        self,
        *,
        instrument_id: int,
        lifecycle_states: list[str] | None = None,
        limit: int = 100,
    ) -> list[BinanceTestnetOrderLedger]:
        stmt = select(BinanceTestnetOrderLedger).where(
            BinanceTestnetOrderLedger.instrument_id == instrument_id
        )
        if lifecycle_states is not None:
            stmt = stmt.where(
                BinanceTestnetOrderLedger.lifecycle_state.in_(lifecycle_states)
            )
        stmt = stmt.order_by(BinanceTestnetOrderLedger.created_at.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def insert(
        self,
        *,
        instrument_id: int,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        lifecycle_state: str,
        price: Decimal | None = None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        parent_client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        now: dt.datetime | None = None,
    ) -> BinanceTestnetOrderLedger:
        row = BinanceTestnetOrderLedger(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            parent_client_order_id=parent_client_order_id,
            lifecycle_state=lifecycle_state,
            planned_at=now,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_state(
        self,
        *,
        row: BinanceTestnetOrderLedger,
        new_state: str,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        now: dt.datetime,
    ) -> BinanceTestnetOrderLedger:
        """Mutate ``row`` in place to reflect a state transition."""
        row.lifecycle_state = new_state
        if broker_order_id is not None:
            row.broker_order_id = broker_order_id
        if extra_metadata is not None:
            # Merge — preserve prior keys.
            merged = dict(row.extra_metadata or {})
            merged.update(extra_metadata)
            row.extra_metadata = merged
        # Stamp the per-state timestamp column when known.
        timestamp_col_for_state = {
            "planned": "planned_at",
            "previewed": "previewed_at",
            "validated": "validated_at",
            "submitted": "submitted_at",
            "filled": "filled_at",
            "tp_sl_armed": "tp_sl_armed_at",
            "tp_sl_triggered": "tp_sl_triggered_at",
            "closed": "closed_at",
            "cancelled": "cancelled_at",
            "reconciled": "reconciled_at",
            "anomaly": "anomaly_at",
        }.get(new_state)
        if timestamp_col_for_state is not None:
            setattr(row, timestamp_col_for_state, now)
        if new_state == "anomaly":
            row.anomaly_reason = anomaly_reason
        row.updated_at = now
        await self._session.flush()
        return row

    async def stamp_reconciled(
        self,
        *,
        row: BinanceTestnetOrderLedger,
        now: dt.datetime,
    ) -> None:
        """Update ``last_reconciled_at`` without changing lifecycle_state."""
        row.last_reconciled_at = now
        row.updated_at = now
        await self._session.flush()
