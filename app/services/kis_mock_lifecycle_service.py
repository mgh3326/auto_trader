"""KIS mock order lifecycle service (ROB-102).

Pure record-keeping. Must not import or call broker mutation services,
KIS live execution, watch alerts, order intents, scheduler, fill
notification, or trade journal code. All writes are atomic per call.

Lifecycle vocabulary follows ROB-100 (`app.schemas.execution_contracts`).
Fine-grained reasoning is stored in `last_reconcile_detail.reason_code`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
from app.schemas.execution_contracts import (
    ORDER_LIFECYCLE_STATES,
    OrderLifecycleState,
    TERMINAL_LIFECYCLE_STATES,
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
        self, *, limit: int = 100
    ) -> list[KISMockOrderLedger]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        stmt = (
            select(KISMockOrderLedger)
            .where(
                KISMockOrderLedger.lifecycle_state.in_(OPEN_LIFECYCLE_STATES)
            )
            .order_by(
                KISMockOrderLedger.trade_date.asc(),
                KISMockOrderLedger.id.asc(),
            )
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

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
            row.reconciled_at = datetime.now(tz=timezone.utc)
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
