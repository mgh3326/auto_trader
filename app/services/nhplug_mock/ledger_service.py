"""The only writer for ``review.nhplug_mock_order_ledger`` (hard rule 5).

Evidence-first rules enforced here, independent of callers:

* a row is created in ``submitting`` and committed *before* the broker leg;
* ``accepted`` needs a broker order number; a row without one can only be
  ``rejected``, ``acceptance_uncertain``, or ``not_submitted``;
* fill quantities and every terminal order state are written only with
  broker listing evidence and ``reconcile_state='verified'``;
* terminal rows never move again.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nhplug_mock_order_ledger import NHPlugMockOrderLedger
from app.services.brokers.nhplug.order_evidence import OrderAck

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"not_submitted", "filled", "cancelled", "modified", "confirmed", "anomaly"}
)
LIVE_ORDER_STATUSES: Final[frozenset[str]] = frozenset(
    {"accepted", "open", "partially_filled"}
)
UNBOUND_UNCERTAIN_STATUSES: Final[frozenset[str]] = frozenset(
    {"submitting", "acceptance_uncertain"}
)
_ORDER_TRACKING_TARGETS: Final[frozenset[str]] = frozenset(
    {"open", "partially_filled", "filled", "cancelled", "modified", "anomaly"}
)
_ALLOWED_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    # A reconcile that finds a row still in ``submitting`` (process died
    # mid-flight) may bind it, confirm a cancel, or flag an anomaly.
    "submitting": frozenset(
        {
            "not_submitted",
            "accepted",
            "rejected",
            "acceptance_uncertain",
            "confirmed",
            "anomaly",
        }
    ),
    "accepted": _ORDER_TRACKING_TARGETS | {"accepted", "rejected", "confirmed"},
    "open": _ORDER_TRACKING_TARGETS,
    "partially_filled": _ORDER_TRACKING_TARGETS - {"open"},
    "acceptance_uncertain": _ORDER_TRACKING_TARGETS
    | {"acceptance_uncertain", "accepted", "confirmed"},
    "rejected": frozenset({"rejected", "anomaly"}),
}
_EVIDENCE_REQUIRED_TARGETS: Final[frozenset[str]] = frozenset(
    {"partially_filled", "filled", "cancelled", "modified", "confirmed"}
)


class NHPlugMockLedgerError(RuntimeError):
    """A ledger write that would violate the evidence-first contract."""


@dataclass(frozen=True, slots=True)
class ReconcileUpdate:
    """One reconcile write; ``status=None`` keeps the current status."""

    reconcile_state: str
    status: str | None = None
    broker_order_id: str | None = None
    filled_qty: int | None = None
    avg_fill_price: Decimal | None = None
    open_qty: int | None = None
    cancelled_qty: int | None = None
    evidence: dict[str, Any] | None = None
    note: dict[str, Any] | None = None
    requires_manual_review: bool = False
    manual_review_reason: str | None = None


class NHPlugMockLedgerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record_submitting(
        self,
        *,
        order_date: str,
        operation_kind: str,
        symbol: str,
        side: str | None,
        quantity: int | None,
        price: int | None,
        original_order_id: str | None = None,
        strategy: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> NHPlugMockOrderLedger:
        """Durably record intent before the broker leg; commit or raise."""

        row = NHPlugMockOrderLedger(
            client_request_id=uuid.uuid4(),
            order_date=order_date,
            # broker/account_mode/venue/order_type come from the model
            # defaults and are pinned by table CHECK constraints.
            operation_kind=operation_kind,
            symbol=symbol,
            side=side,
            quantity=None if quantity is None else Decimal(quantity),
            price=None if price is None else Decimal(price),
            original_order_id=original_order_id,
            status="submitting",
            reconcile_state="pending",
            strategy=strategy,
            reason=reason,
            correlation_id=correlation_id,
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def record_not_submitted(
        self, row_id: int, *, refusal: str
    ) -> NHPlugMockOrderLedger:
        row = await self._require(row_id)
        self._transition(row, "not_submitted")
        row.response_code = "not_submitted"
        row.response_message = refusal[:160]
        return await self._commit(row)

    async def record_ack(self, row_id: int, ack: OrderAck) -> NHPlugMockOrderLedger:
        row = await self._require(row_id)
        if ack.state == "accepted":
            if not ack.broker_order_id:
                raise NHPlugMockLedgerError("accepted needs a broker order number")
            row.broker_order_id = ack.broker_order_id
        self._transition(row, ack.state)
        row.response_code = ack.response_code
        row.response_message = ack.response_message
        if ack.state == "acceptance_uncertain":
            row.requires_manual_review = True
            row.manual_review_reason = "order_acceptance_uncertain"
        return await self._commit(row)

    async def record_dispatch_uncertain(self, row_id: int) -> NHPlugMockOrderLedger:
        row = await self._require(row_id)
        self._transition(row, "acceptance_uncertain")
        row.response_code = "dispatch_uncertain"
        row.requires_manual_review = True
        row.manual_review_reason = "order_dispatch_outcome_unknown"
        return await self._commit(row)

    async def apply_reconcile(
        self, row_id: int, update: ReconcileUpdate
    ) -> NHPlugMockOrderLedger:
        row = await self._require(row_id)
        if row.status in TERMINAL_STATUSES:
            raise NHPlugMockLedgerError("terminal ledger rows are immutable")
        target = update.status or row.status
        if target in _EVIDENCE_REQUIRED_TARGETS:
            if update.reconcile_state != "verified" or not update.evidence:
                raise NHPlugMockLedgerError(
                    f"{target} requires verified broker listing evidence"
                )
        carries_evidence_fields = any(
            value is not None
            for value in (
                update.filled_qty,
                update.avg_fill_price,
                update.open_qty,
                update.cancelled_qty,
                update.evidence,
            )
        )
        if carries_evidence_fields and (
            update.reconcile_state != "verified" or not update.evidence
        ):
            raise NHPlugMockLedgerError(
                "quantities and evidence are written only with verified broker "
                "listing evidence"
            )
        if update.broker_order_id is not None:
            if row.broker_order_id not in {None, update.broker_order_id}:
                raise NHPlugMockLedgerError("broker order number cannot be rebound")
            row.broker_order_id = update.broker_order_id
        if target != row.status:
            self._transition(row, target)
        row.reconcile_state = update.reconcile_state
        if update.filled_qty is not None:
            row.filled_qty = Decimal(update.filled_qty)
            row.avg_fill_price = update.avg_fill_price
        if update.open_qty is not None:
            row.open_qty = Decimal(update.open_qty)
        if update.cancelled_qty is not None:
            row.cancelled_qty = Decimal(update.cancelled_qty)
        if update.evidence is not None:
            row.evidence = update.evidence
        row.last_reconcile = {
            "at": datetime.now(UTC).isoformat(),
            "reconcile_state": update.reconcile_state,
            **(update.note or {}),
        }
        if update.requires_manual_review:
            row.requires_manual_review = True
            row.manual_review_reason = update.manual_review_reason
        row.reconciled_at = datetime.now(UTC)
        return await self._commit(row)

    async def get(self, row_id: int) -> NHPlugMockOrderLedger | None:
        return await self._db.get(NHPlugMockOrderLedger, row_id)

    async def list_for_date(self, order_date: str) -> Sequence[NHPlugMockOrderLedger]:
        result = await self._db.execute(
            select(NHPlugMockOrderLedger)
            .where(NHPlugMockOrderLedger.order_date == order_date)
            .order_by(NHPlugMockOrderLedger.id)
        )
        return result.scalars().all()

    async def find_by_broker_order_id(
        self, *, order_date: str, broker_order_id: str
    ) -> NHPlugMockOrderLedger | None:
        result = await self._db.execute(
            select(NHPlugMockOrderLedger).where(
                NHPlugMockOrderLedger.order_date == order_date,
                NHPlugMockOrderLedger.broker_order_id == broker_order_id,
            )
        )
        return result.scalar_one_or_none()

    async def _require(self, row_id: int) -> NHPlugMockOrderLedger:
        row = await self._db.get(NHPlugMockOrderLedger, row_id)
        if row is None:
            raise NHPlugMockLedgerError("ledger row not found")
        return row

    @staticmethod
    def _transition(row: NHPlugMockOrderLedger, target: str) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(row.status, frozenset())
        if target not in allowed:
            raise NHPlugMockLedgerError(
                f"illegal ledger transition {row.status} -> {target}"
            )
        row.status = target

    async def _commit(self, row: NHPlugMockOrderLedger) -> NHPlugMockOrderLedger:
        await self._db.flush()
        await self._db.commit()
        await self._db.refresh(row)
        return row
