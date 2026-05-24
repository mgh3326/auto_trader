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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument

# Lifecycle states that block starting a new lifecycle for a symbol: a
# row is either in flight (planned..filled) or in an unresolved anomaly.
# closed / reconciled / cancelled free the slot (cooldown then spaces
# re-entry). Single source of truth for read-side "is this open?".
OPEN_LIFECYCLE_STATES: tuple[str, ...] = (
    "planned",
    "previewed",
    "validated",
    "submitted",
    "filled",
    "anomaly",
)


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
        """Insert a new ledger row in the ``planned`` lifecycle state."""
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
        await self._session.refresh(row)
        return row

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceDemoOrderLedger | None:
        """Return the row matching ``client_order_id`` or ``None``."""
        stmt = select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == client_order_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Read-only queries (ROB-307 ledger-backed durable scalping state §4).
    # ------------------------------------------------------------------

    async def resolve_instrument_id(
        self, *, venue: str, product: str, venue_symbol: str
    ) -> int | None:
        """Map a ``(venue, product, venue_symbol)`` triple to instrument id."""
        return await self._session.scalar(
            select(CryptoInstrument.id).where(
                CryptoInstrument.venue == venue,
                CryptoInstrument.product == product,
                CryptoInstrument.venue_symbol == venue_symbol,
            )
        )

    async def count_open_lifecycles(self) -> int:
        """Count table-wide rows in an open/blocking lifecycle state."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(BinanceDemoOrderLedger.lifecycle_state.in_(OPEN_LIFECYCLE_STATES))
        )
        return count or 0

    async def has_open_lifecycle_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> bool:
        count = await self._session.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(
                BinanceDemoOrderLedger.product == product,
                BinanceDemoOrderLedger.instrument_id == instrument_id,
                BinanceDemoOrderLedger.lifecycle_state.in_(OPEN_LIFECYCLE_STATES),
            )
        )
        return (count or 0) > 0

    async def count_lifecycles_since(self, *, since: dt.datetime) -> int:
        """Count lifecycles initiated (``planned_at``) at or after ``since``."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(BinanceDemoOrderLedger.planned_at >= since)
        )
        return count or 0

    async def latest_close_at_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> dt.datetime | None:
        return await self._session.scalar(
            select(func.max(BinanceDemoOrderLedger.closed_at)).where(
                BinanceDemoOrderLedger.product == product,
                BinanceDemoOrderLedger.instrument_id == instrument_id,
            )
        )

    async def closed_rows_since(
        self, *, since: dt.datetime
    ) -> list[BinanceDemoOrderLedger]:
        result = await self._session.execute(
            select(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.closed_at >= since
            )
        )
        return list(result.scalars().all())

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
        """Mutate ``row`` in place to reflect a lifecycle state transition."""
        row.lifecycle_state = new_state
        row.updated_at = now
        if broker_order_id is not None:
            row.broker_order_id = broker_order_id
        if anomaly_reason is not None:
            row.anomaly_reason = anomaly_reason
        # Stamp the per-state timestamp column when known. Adding a new
        # lifecycle state (e.g., PR 2 futures states) is a one-line change
        # below — and the model must grow the matching column first.
        timestamp_col_for_state = {
            "planned": "planned_at",
            "previewed": "previewed_at",
            "validated": "validated_at",
            "submitted": "submitted_at",
            "filled": "filled_at",
            "closed": "closed_at",
            "cancelled": "cancelled_at",
            "reconciled": "reconciled_at",
            "anomaly": "anomaly_at",
        }.get(new_state)
        if timestamp_col_for_state is not None:
            setattr(row, timestamp_col_for_state, now)
        # ``reconciled`` additionally stamps ``last_reconciled_at`` so
        # repeat reconciliations can refresh the freshness signal.
        if new_state == "reconciled":
            row.last_reconciled_at = now
        if extra_metadata_merge is not None:
            merged = dict(row.extra_metadata or {})
            merged.update(extra_metadata_merge)
            row.extra_metadata = merged
        await self._session.flush()
        return row
