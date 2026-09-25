"""The only writer for ``review.nhplug_mock_order_ledger`` (hard rule 5).

Evidence-first rules enforced here, independent of callers:

* a row is created in ``submitting`` and committed *before* the broker leg;
* immediately before send the row is atomically claimed (``dispatching``)
  by ``claim_for_dispatch`` — one conditional UPDATE committed before any byte
  leaves — and the request body is built only from the claimed values, so a
  row can be dispatched at most once and only with its committed body;
* an unclaimed ``submitting`` row therefore provably never reached send;
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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nhplug_mock_order_ledger import NHPlugMockOrderLedger
from app.services.brokers.nhplug.contracts import ClaimedOrder, ExpectedOrder
from app.services.brokers.nhplug.order_evidence import OrderAck

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"not_submitted", "filled", "cancelled", "modified", "confirmed", "anomaly"}
)
LIVE_ORDER_STATUSES: Final[frozenset[str]] = frozenset(
    {"accepted", "open", "partially_filled"}
)
UNBOUND_UNCERTAIN_STATUSES: Final[frozenset[str]] = frozenset(
    {"dispatching", "acceptance_uncertain"}
)
_ORDER_TRACKING_TARGETS: Final[frozenset[str]] = frozenset(
    {"open", "partially_filled", "filled", "cancelled", "modified", "anomaly"}
)
_ALLOWED_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    # ``submitting`` leaves only through the atomic claim (to
    # ``dispatching``, not via ``_transition``) or a pre-send refusal.
    "submitting": frozenset({"not_submitted"}),
    # A claimed row records its broker outcome; a reconcile that finds one
    # still ``dispatching`` (process died after the claim) may bind it,
    # confirm a cancel, or flag an anomaly.
    "dispatching": frozenset(
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
        return await self._commit(row)

    async def claim_for_dispatch(
        self, *, row_id: int, client_request_id: str, expected: ExpectedOrder
    ) -> ClaimedOrder | None:
        """Atomically claim one committed ``submitting`` row for its only send.

        A single conditional UPDATE moves the row to ``dispatching`` only if
        the id, client_request_id, every order field, the ``submitting``
        status and an empty claim all match; it is committed before return.
        Returns the claimed values (the only source of the request body), or
        ``None`` when nothing matched — a replay, another client, a
        concurrent call, or a drifted expectation.
        """

        model = NHPlugMockOrderLedger
        try:
            request_uuid = uuid.UUID(str(client_request_id))
        except ValueError:
            return None
        original = (
            None
            if expected.original_order_no is None
            else str(expected.original_order_no)
        )
        statement = (
            update(model)
            .where(
                model.id == row_id,
                model.client_request_id == request_uuid,
                model.status == "submitting",
                model.claim_token.is_(None),
                model.operation_kind == expected.operation,
                model.symbol == expected.symbol,
                model.side.is_not_distinct_from(expected.side),
                model.quantity.is_not_distinct_from(
                    None if expected.quantity is None else Decimal(expected.quantity)
                ),
                model.price.is_not_distinct_from(
                    None if expected.price is None else Decimal(expected.price)
                ),
                model.original_order_id.is_not_distinct_from(original),
            )
            .values(
                status="dispatching",
                claim_token=uuid.uuid4(),
                claimed_at=func.now(),
                updated_at=func.now(),
            )
            .returning(
                model.id,
                model.client_request_id,
                model.claim_token,
                model.operation_kind,
                model.symbol,
                model.side,
                model.quantity,
                model.price,
                model.original_order_id,
            )
            .execution_options(synchronize_session=False)
        )
        try:
            claimed = (await self._db.execute(statement)).one_or_none()
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        if claimed is None:
            return None
        return ClaimedOrder(
            ledger_row_id=claimed.id,
            client_request_id=str(claimed.client_request_id),
            claim_token=str(claimed.claim_token),
            operation=claimed.operation_kind,
            symbol=claimed.symbol,
            side=claimed.side,
            quantity=None if claimed.quantity is None else int(claimed.quantity),
            price=None if claimed.price is None else int(claimed.price),
            original_order_no=int(claimed.original_order_id)
            if claimed.original_order_id
            else None,
        )

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
            row.ack_order_id = ack.broker_order_id
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
        result = await self._db.execute(
            select(NHPlugMockOrderLedger)
            .where(NHPlugMockOrderLedger.id == row_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_for_date(self, order_date: str) -> Sequence[NHPlugMockOrderLedger]:
        result = await self._db.execute(
            select(NHPlugMockOrderLedger)
            .where(NHPlugMockOrderLedger.order_date == order_date)
            .order_by(NHPlugMockOrderLedger.id)
            .execution_options(populate_existing=True)
        )
        return result.scalars().all()

    async def find_by_broker_order_id(
        self, *, order_date: str, broker_order_id: str
    ) -> NHPlugMockOrderLedger | None:
        result = await self._db.execute(
            select(NHPlugMockOrderLedger)
            .where(
                NHPlugMockOrderLedger.order_date == order_date,
                NHPlugMockOrderLedger.broker_order_id == broker_order_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def _require(self, row_id: int) -> NHPlugMockOrderLedger:
        # Always re-read: the dispatch claim is a Core UPDATE, so a cached
        # identity-map instance may still show ``submitting``.
        result = await self._db.execute(
            select(NHPlugMockOrderLedger)
            .where(NHPlugMockOrderLedger.id == row_id)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
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
        """Commit one write; on failure roll back so the session stays usable.

        Callers share one session across a round trip or a reconcile pass;
        a failed flush (e.g. a duplicate broker order number) must not leave
        it in a rollback-required state that blocks every later write.
        """

        try:
            await self._db.flush()
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        await self._db.refresh(row)
        return row
