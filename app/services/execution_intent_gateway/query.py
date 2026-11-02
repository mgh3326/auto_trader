"""ROB-1461 — reconcile an intent from its journal and ledger evidence. Read only.

:func:`build_reconciliation` is pure. It answers E2's ``Query``: the intent's
working order and the filled quantity, or that it cannot say.

An unknown outcome stays unknown without positive evidence. No ledger row is not
proof that no order exists, because the ledger row is written after the send. So
``view`` is ``None`` in that case and the executor keeps holding. A wrong
"no working order" would be worse: it could leave a live order behind.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import trade_day_kst
from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.review import KISMockOrderLedger
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    _root_of,
)
from app.services.execution_intent_gateway.kis_mock_forwarder import (
    OPEN_LEDGER_STATES,
    order_correlation_id,
)

_UNKNOWN_STATES: Final = frozenset({ChildState.SENDING, ChildState.UNCERTAIN})

#: One KIS order: its number and its KST trading day (the ROB-1379 identity). KIS
#: reuses numbers across days, so a number alone does not name an order.
type OrderDay = tuple[str, str]


class UnresolvedReason(StrEnum):
    NO_LEDGER_EVIDENCE = "no_ledger_evidence"
    LEDGER_ROW_AMBIGUOUS = "ledger_row_ambiguous"
    LEDGER_STATE_UNRESOLVED = "ledger_state_unresolved"
    MULTIPLE_WORKING_ORDERS = "multiple_working_orders"


@dataclass(frozen=True, slots=True)
class LedgerOrder:
    """One ``kis_mock_order_ledger`` row, as evidence for one order lineage."""

    order_no: str | None
    status: str
    lifecycle_state: str
    quantity: int
    filled_qty: int
    broker_cancel_confirmed: bool | None = None


@dataclass(frozen=True, slots=True)
class LedgerEvidence:
    orders_by_root: Mapping[str, tuple[LedgerOrder, ...]] = field(default_factory=dict)
    filled_by_order_day: Mapping[OrderDay, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    key: str
    price: int
    qty: int


@dataclass(frozen=True, slots=True)
class Unresolved:
    idempotency_key: str
    state: str
    reason: UnresolvedReason


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """``reconciled`` is true only when ``order`` and ``filled_qty`` can be trusted."""

    reconciled: bool
    order: WorkingOrder | None
    filled_qty: int
    unresolved: tuple[Unresolved, ...]


def _lineages(children: Sequence[ChildSnapshot]) -> dict[str, list[ChildSnapshot]]:
    by_key = {child.idempotency_key: child for child in children}
    lineages: dict[str, list[ChildSnapshot]] = {}
    for child in children:
        if child.state is not ChildState.REFUSED:
            root = _root_of(child.idempotency_key, by_key)
            lineages.setdefault(root, []).append(child)
    return lineages


def _lineage_orders(
    rows: Sequence[ChildSnapshot], ledger_orders: Sequence[LedgerOrder]
) -> set[OrderDay]:
    """The orders this lineage placed.

    A submit or an amend places an order on the day it was sent. A cancel journals
    its target's number on its own day, so it places nothing. A number known only
    from the ledger belongs to a sender with no recorded number.
    """
    senders = [row for row in rows if row.action != "cancel"]
    journaled = {
        (row.broker_order_id, trade_day_kst(row.created_at))
        for row in senders
        if row.broker_order_id
    }
    journaled_numbers = {number for number, _ in journaled}
    unnumbered_days = {
        trade_day_kst(row.created_at) for row in senders if not row.broker_order_id
    }
    ledger_only = {
        (order.order_no, day)
        for order in ledger_orders
        if order.order_no and order.order_no not in journaled_numbers
        for day in unnumbered_days
    }
    return journaled | ledger_only


def _lineage_filled(
    rows: Sequence[ChildSnapshot],
    ledger_orders: Sequence[LedgerOrder],
    filled_by_order_day: Mapping[OrderDay, int],
) -> int:
    executed = sum(
        filled_by_order_day.get(order, 0)
        for order in _lineage_orders(rows, ledger_orders)
    )
    return max(executed, max((o.filled_qty for o in ledger_orders), default=0))


def _ledger_proves_closed(ledger_order: LedgerOrder, filled: int) -> bool:
    if ledger_order.status == "rejected" or ledger_order.lifecycle_state == "expired":
        return True
    if ledger_order.lifecycle_state == "cancelled":
        # A soft cancel leaves the order possibly live at the broker.
        return ledger_order.broker_cancel_confirmed is True
    return 0 < ledger_order.quantity <= filled


def _resolve_lineage(
    rows: Sequence[ChildSnapshot],
    ledger_order: LedgerOrder | None,
    filled: int,
) -> tuple[WorkingOrder | None, tuple[Unresolved, ...]]:
    last = rows[-1]
    if last.action == "cancel" and last.state is ChildState.ACCEPTED:
        return None, ()
    if ledger_order is not None and _ledger_proves_closed(ledger_order, filled):
        return None, ()
    if (
        ledger_order is not None
        and ledger_order.lifecycle_state not in OPEN_LEDGER_STATES
    ):
        return None, tuple(
            Unresolved(
                row.idempotency_key,
                row.state.value,
                UnresolvedReason.LEDGER_STATE_UNRESOLVED,
            )
            for row in rows
        )

    unknown = [
        row
        for row in rows
        if row.state in _UNKNOWN_STATES
        and not (
            # The ledger row of a submit is written from the broker's answer, so
            # an accepted row proves the order exists under the submit's key.
            row.action == "submit"
            and ledger_order is not None
            and ledger_order.status == "accepted"
            and ledger_order.order_no
        )
    ]
    if unknown:
        return None, tuple(
            Unresolved(
                row.idempotency_key,
                row.state.value,
                UnresolvedReason.NO_LEDGER_EVIDENCE,
            )
            for row in unknown
        )

    working = next(row for row in reversed(rows) if row.action != "cancel")
    if working.quantity is None or working.limit_price_krw is None:
        raise ValueError("a journaled submit or amend has no quantity or price")
    if working.quantity <= filled:
        return None, ()
    return (
        WorkingOrder(
            key=working.idempotency_key,
            price=working.limit_price_krw,
            qty=working.quantity,
        ),
        (),
    )


def build_reconciliation(
    children: Sequence[ChildSnapshot], evidence: LedgerEvidence
) -> Reconciliation:
    """Fold the journal and the ledger evidence into E2's view of the intent."""
    working: list[WorkingOrder] = []
    unresolved: list[Unresolved] = []
    filled_total = 0
    for root, rows in _lineages(children).items():
        ledger_rows = evidence.orders_by_root.get(root, ())
        if len(ledger_rows) > 1:
            unresolved.extend(
                Unresolved(
                    row.idempotency_key,
                    row.state.value,
                    UnresolvedReason.LEDGER_ROW_AMBIGUOUS,
                )
                for row in rows
            )
            continue
        ledger_order = ledger_rows[0] if ledger_rows else None
        filled = _lineage_filled(rows, ledger_rows, evidence.filled_by_order_day)
        filled_total += filled
        order, lineage_unresolved = _resolve_lineage(rows, ledger_order, filled)
        unresolved.extend(lineage_unresolved)
        if order is not None:
            working.append(order)

    if len(working) > 1:
        # E2 tracks one working order. Naming one here would orphan the others.
        by_key = {child.idempotency_key: child for child in children}
        unresolved.extend(
            Unresolved(
                order.key,
                by_key[order.key].state.value,
                UnresolvedReason.MULTIPLE_WORKING_ORDERS,
            )
            for order in working
        )
    if unresolved:
        return Reconciliation(False, None, filled_total, tuple(unresolved))
    return Reconciliation(True, working[0] if working else None, filled_total, ())


class LedgerEvidenceReader(Protocol):
    async def read(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> LedgerEvidence: ...


def _whole_shares_up(value: Any) -> int:
    return math.ceil(Decimal(str(value)))


def order_days_of(
    lineages: Mapping[str, Sequence[ChildSnapshot]],
    orders_by_root: Mapping[str, Sequence[LedgerOrder]],
) -> set[OrderDay]:
    """Every order the intent's lineages placed."""
    return {
        order
        for root, rows in lineages.items()
        for order in _lineage_orders(rows, orders_by_root.get(root, ()))
    }


def executed_by_order_day(
    rows: Iterable[tuple[str, Any, str, datetime]],
    order_days: Collection[OrderDay],
) -> dict[OrderDay, int]:
    """Executed quantity per order, from ``(id, qty, source, filled_at)``.

    This value can close an order, so a fill counts only for the order it belongs
    to. Otherwise an old fill under a reused number would report a live order as
    finished.
    """
    incremental: dict[OrderDay, Decimal] = {}
    cumulative: dict[OrderDay, Decimal] = {}
    for order_id, quantity, source, filled_at in rows:
        order = (order_id, trade_day_kst(filled_at))
        if order not in order_days:
            continue
        amount = Decimal(str(quantity))
        if source == "websocket":
            incremental[order] = incremental.get(order, Decimal(0)) + amount
        else:
            cumulative[order] = max(cumulative.get(order, Decimal(0)), amount)
    return {
        order: _whole_shares_up(
            max(
                incremental.get(order, Decimal(0)),
                cumulative.get(order, Decimal(0)),
            )
        )
        for order in incremental.keys() | cumulative.keys()
    }


def _detail_flag(detail: Any) -> bool | None:
    if isinstance(detail, Mapping):
        flag = detail.get("broker_cancel_confirmed")
        if isinstance(flag, bool):
            return flag
    return None


class SqlLedgerEvidenceReader:
    """Reads ledger evidence in a ``READ ONLY`` transaction. Calls no broker."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> LedgerEvidence:
        from app.mcp_server.tooling.kis_mock_ledger import _derive_shadow_fill

        lineages = _lineages(children)
        if not lineages:
            return LedgerEvidence()
        root_by_correlation = {order_correlation_id(root): root for root in lineages}

        orders_by_root: dict[str, list[LedgerOrder]] = {}
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = await session.scalars(
                select(KISMockOrderLedger).where(
                    KISMockOrderLedger.correlation_id.in_(list(root_by_correlation))
                )
            )
            for row in rows:
                ordered = float(row.quantity)
                filled, _, _ = _derive_shadow_fill(row, ordered)
                orders_by_root.setdefault(
                    root_by_correlation[str(row.correlation_id)], []
                ).append(
                    LedgerOrder(
                        order_no=row.order_no,
                        status=row.status,
                        lifecycle_state=row.lifecycle_state,
                        quantity=_whole_shares_up(row.quantity),
                        filled_qty=_whole_shares_up(filled),
                        broker_cancel_confirmed=_detail_flag(row.last_reconcile_detail),
                    )
                )
            filled_by_order_day = await self._executions(
                session,
                symbol=intent.symbol,
                order_days=order_days_of(lineages, orders_by_root),
            )
            await session.rollback()
        return LedgerEvidence(
            orders_by_root={key: tuple(value) for key, value in orders_by_root.items()},
            filled_by_order_day=filled_by_order_day,
        )

    @staticmethod
    async def _executions(
        session: AsyncSession, *, symbol: str, order_days: Collection[OrderDay]
    ) -> dict[OrderDay, int]:
        if not order_days:
            return {}
        rows = await session.execute(
            select(
                ExecutionLedger.broker_order_id,
                ExecutionLedger.filled_qty,
                ExecutionLedger.source,
                ExecutionLedger.filled_at,
            ).where(
                ExecutionLedger.broker == "kis",
                ExecutionLedger.account_mode == "mock",
                ExecutionLedger.symbol == symbol,
                ExecutionLedger.broker_order_id.in_(
                    list({number for number, _ in order_days})
                ),
                execution_ledger_in_effect(),
            )
        )
        return executed_by_order_day(rows.tuples().all(), order_days)


def child_json(child: ChildSnapshot) -> dict[str, Any]:
    """One journal row as recorded. Carries no broker ``result``."""
    return {
        "child_id": child.child_id,
        "idempotency_key": child.idempotency_key,
        "action": child.action,
        "target_idempotency_key": child.target_idempotency_key,
        "quantity": child.quantity,
        "limit_price_krw": child.limit_price_krw,
        "state": child.state.value,
        "refusal_reason": child.refusal_reason,
        "broker_order_id": child.broker_order_id,
    }


def reconciliation_json(
    intent_id: UUID, children: Sequence[ChildSnapshot], reconciliation: Reconciliation
) -> dict[str, Any]:
    """The query answer. ``view`` has exactly the fields of fillwire's ``exec.View``."""
    view: dict[str, Any] | None = None
    if reconciliation.reconciled:
        view = {"filled_qty": reconciliation.filled_qty}
        order = reconciliation.order
        if order is not None:
            view["order"] = {"key": order.key, "price": order.price, "qty": order.qty}
    return {
        "intent_id": str(intent_id),
        "reconciled": reconciliation.reconciled,
        "view": view,
        "unresolved": [
            {
                "idempotency_key": item.idempotency_key,
                "state": item.state,
                "reason": item.reason.value,
            }
            for item in reconciliation.unresolved
        ],
        "children": [child_json(child) for child in children],
    }
