"""KIS mock order lifecycle service (ROB-102).

Pure record-keeping. Must not import or call broker mutation services,
KIS live execution, watch alerts, order intents, scheduler, fill
notification, or trade journal code. All writes are atomic per call.

Lifecycle vocabulary follows ROB-100 (`app.schemas.execution_contracts`).
Fine-grained reasoning is stored in `last_reconcile_detail.reason_code`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
from app.schemas.execution_contracts import (
    ORDER_LIFECYCLE_STATES,
    TERMINAL_LIFECYCLE_STATES,
    OrderLifecycleState,
)

# These are the lifecycle states the reconciler reads from. anything
# else is excluded from `list_open_orders`.
OPEN_LIFECYCLE_STATES: frozenset[str] = frozenset({"accepted", "pending", "fill"})


class LedgerNotFoundError(Exception):
    """Raised when a ledger row with the given id does not exist."""


class KISMockLifecycleService:
    """Pure record-keeping service for KIS mock order lifecycle."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_open_orders(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        instrument_type: str | None = None,
        side: str | None = None,
    ) -> list[KISMockOrderLedger]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        stmt = select(KISMockOrderLedger).where(
            KISMockOrderLedger.lifecycle_state.in_(OPEN_LIFECYCLE_STATES)
        )
        if symbol:
            stmt = stmt.where(KISMockOrderLedger.symbol == symbol)
        if instrument_type:
            stmt = stmt.where(KISMockOrderLedger.instrument_type == instrument_type)
        if side:
            stmt = stmt.where(KISMockOrderLedger.side == side)
        stmt = stmt.order_by(
            KISMockOrderLedger.trade_date.asc(),
            KISMockOrderLedger.id.asc(),
        ).limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_order_no(
        self, *, order_no: str
    ) -> KISMockOrderLedger | None:
        """Look up a single ledger row by broker order number.

        Used by cancel/modify so KIS mock never depends on the unsupported
        TTTC8036R pending-orders inquiry.
        """
        stmt = select(KISMockOrderLedger).where(
            KISMockOrderLedger.order_no == order_no
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_order_terms(
        self,
        *,
        ledger_id: int,
        price: Decimal | None = None,
        quantity: Decimal | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Reflect a broker-confirmed modify on the ledger row."""
        row = await self._db.get(KISMockOrderLedger, ledger_id)
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))
        if price is not None:
            row.price = price
        if quantity is not None:
            row.quantity = quantity
        if detail is not None:
            row.last_reconcile_detail = {
                **(row.last_reconcile_detail or {}),
                **detail,
            }
        await self._db.commit()

    async def record_holdings_baseline(
        self,
        *,
        ledger_id: int,
        baseline_qty: Decimal,
    ) -> None:
        row = await self._db.get(KISMockOrderLedger, ledger_id)
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))
        row.holdings_baseline_qty = baseline_qty
        await self._db.commit()

    async def apply_lifecycle_transition(
        self,
        *,
        ledger_id: int,
        next_state: OrderLifecycleState,
        reason_code: str,
        detail: dict[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        if next_state not in ORDER_LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state: {next_state!r}")

        row = await self._db.get(KISMockOrderLedger, ledger_id)
        if row is None:
            raise LedgerNotFoundError(str(ledger_id))

        prior_state = row.lifecycle_state
        would_change = prior_state != next_state

        if dry_run:
            return {
                "ledger_id": ledger_id,
                "prior_state": prior_state,
                "next_state": next_state,
                "reason_code": reason_code,
                "would_change": would_change,
                "applied": False,
                "dry_run": True,
            }

        if not would_change:
            row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
            row.last_reconcile_detail = {"reason_code": reason_code, **detail}
            await self._db.commit()
            return {
                "ledger_id": ledger_id,
                "prior_state": prior_state,
                "next_state": next_state,
                "reason_code": reason_code,
                "applied": True,
                "would_change": False,
                "dry_run": False,
            }

        row.lifecycle_state = next_state
        row.reconcile_attempts = (row.reconcile_attempts or 0) + 1
        row.last_reconcile_detail = {"reason_code": reason_code, **detail}
        if next_state in TERMINAL_LIFECYCLE_STATES:
            row.reconciled_at = datetime.now(tz=UTC)
        await self._db.commit()

        return {
            "ledger_id": ledger_id,
            "prior_state": prior_state,
            "next_state": next_state,
            "reason_code": reason_code,
            "applied": True,
            "would_change": True,
            "dry_run": False,
        }


__all__ = [
    "KISMockLifecycleService",
    "LedgerNotFoundError",
    "OPEN_LIFECYCLE_STATES",
]
