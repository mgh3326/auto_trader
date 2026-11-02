"""ROB-1461 — reconcile an intent from its journal and ledger evidence. Read only.

:func:`build_reconciliation` is pure. It answers E2's ``Query``: the intent's
working order and the filled quantity, or that it cannot say.

An unknown outcome stays unknown without positive evidence. No ledger row is not
proof that no order exists, because the ledger row is written after the send. So
``view`` is ``None`` in that case and the executor keeps holding. A wrong
"no working order" would be worse: it could leave a live order behind.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

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
from app.services.execution_ledger.supersede import supersede_provisional

_UNKNOWN_STATES: Final = frozenset({ChildState.SENDING, ChildState.UNCERTAIN})

#: One KIS order: its number and its KST trading day (the ROB-1379 identity). KIS
#: reuses numbers across days, so a number alone does not name an order.
type OrderDay = tuple[str, str]


def _order_number(raw: object) -> str:
    """Writers differ on leading zeros. The same rule as ROB-1379's identity."""
    number = str(raw or "").strip()
    return number.lstrip("0") or ("0" if number else "")


class UnresolvedReason(StrEnum):
    NO_LEDGER_EVIDENCE = "no_ledger_evidence"
    LEDGER_ROW_AMBIGUOUS = "ledger_row_ambiguous"
    LEDGER_STATE_UNRESOLVED = "ledger_state_unresolved"
    MULTIPLE_WORKING_ORDERS = "multiple_working_orders"
    ORDER_IDENTITY_AMBIGUOUS = "order_identity_ambiguous"
    FILL_EVIDENCE_CONFLICT = "fill_evidence_conflict"


@dataclass(frozen=True, slots=True)
class LedgerOrder:
    """One ``kis_mock_order_ledger`` row, as evidence for one order lineage.

    ``filled_qty`` is the holdings reconciler's estimate, and ``None`` when the row
    claims a fill without a readable quantity. It is shared out from a whole symbol's
    holdings change, so it proves nothing about this order. It can only conflict
    with the execution ledger, never close an order.
    """

    order_no: str | None
    status: str
    lifecycle_state: str
    filled_qty: int | None
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
    """``filled_qty`` is ``None`` unless reconciled: without every lineage it is partial."""

    reconciled: bool
    order: WorkingOrder | None
    filled_qty: int | None
    unresolved: tuple[Unresolved, ...]


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    key: OrderDay
    quantity: int


@dataclass(frozen=True, slots=True)
class LineageOrders:
    """The only statement of which orders a lineage placed.

    The evidence reader loads fills for ``placed``, and closure reads fills for
    ``current``, so closure can never ask about an order the reader did not load.
    """

    placed: tuple[PlacedOrder, ...]
    #: The order the last submit or amend placed, when it can be named.
    current: PlacedOrder | None
    identity_known: bool
    #: An amend with an unknown outcome that no evidence names. It may have placed a
    #: replacement, and anything the ledger says is about the order before it.
    replacement_unseen: bool = False


def _lineages(children: Sequence[ChildSnapshot]) -> dict[str, list[ChildSnapshot]]:
    by_key = {child.idempotency_key: child for child in children}
    lineages: dict[str, list[ChildSnapshot]] = {}
    for child in sorted(children, key=lambda row: row.child_id):
        if child.state is not ChildState.REFUSED:
            root = _root_of(child.idempotency_key, by_key)
            lineages.setdefault(root, []).append(child)
    return lineages


def _senders(rows: Sequence[ChildSnapshot]) -> list[ChildSnapshot]:
    return [row for row in rows if row.action != "cancel"]


def _sent_quantity(row: ChildSnapshot) -> int:
    if row.quantity is None:
        raise ValueError("a journaled submit or amend has no quantity")
    return row.quantity


def _single(rows: Sequence[LedgerOrder]) -> LedgerOrder | None:
    return rows[0] if len(rows) == 1 else None


def lineage_orders(
    rows: Sequence[ChildSnapshot], ledger_order: LedgerOrder | None
) -> LineageOrders:
    """Name the orders of one lineage.

    A submit or an amend places an order on the day it was sent, and a cancel places
    nothing. E1 accepts a sender only with a number and amends only an accepted one,
    so only the last sender can lack a number: the ledger names that order if it
    followed the send. Its quantity is always what the journal sent. Anything else
    cannot be told apart from a stale or reused number, so it is not an identity to
    count fills against.
    """
    senders = _senders(rows)
    if not senders:
        raise ValueError("a lineage has no journaled submit or amend")
    last = senders[-1]
    numbered = [row for row in senders if _order_number(row.broker_order_id)]
    placed = [
        PlacedOrder(
            (_order_number(row.broker_order_id), trade_day_kst(row.created_at)),
            _sent_quantity(row),
        )
        for row in numbered
    ]
    ledger_number = (
        _order_number(ledger_order.order_no) if ledger_order is not None else ""
    )
    numbers = {order.key[0] for order in placed}

    if _order_number(last.broker_order_id):
        return LineageOrders(
            tuple(placed),
            placed[-1],
            len(numbered) == len(senders) and ledger_number in ("", placed[-1].key[0]),
        )

    identity_known = len(numbered) == len(senders) - 1
    if ledger_order is None or not ledger_number:
        return LineageOrders(
            tuple(placed), None, identity_known, last.action == "amend"
        )
    key = (ledger_number, trade_day_kst(last.created_at))
    if placed and placed[-1].key == key:
        # The ledger did not follow the send, so it still names the previous order.
        return LineageOrders(tuple(placed), None, identity_known, True)
    if ledger_number in numbers:
        # An earlier order's number on another day: either the send never applied or
        # the broker reused the number, and fills under it may be a stranger's.
        return LineageOrders(tuple(placed), None, False)
    current = PlacedOrder(key, _sent_quantity(last))
    return LineageOrders((*placed, current), current, identity_known)


def _unresolved(
    rows: Iterable[ChildSnapshot], reason: UnresolvedReason
) -> tuple[Unresolved, ...]:
    return tuple(
        Unresolved(row.idempotency_key, row.state.value, reason) for row in rows
    )


def _ledger_proves_gone(
    ledger_order: LedgerOrder, rows: Sequence[ChildSnapshot]
) -> bool:
    if ledger_order.status == "rejected":
        # A broker number means the broker took the order, whatever the row says.
        return not _order_number(ledger_order.order_no) and not any(
            _order_number(row.broker_order_id) for row in _senders(rows)
        )
    if ledger_order.lifecycle_state == "expired":
        return True
    if ledger_order.lifecycle_state == "cancelled":
        # A soft cancel leaves the order possibly live at the broker.
        return ledger_order.broker_cancel_confirmed is True
    return False


def _fills_conflict(
    orders: LineageOrders,
    ledger_order: LedgerOrder | None,
    filled_by_order_day: Mapping[OrderDay, int],
) -> bool:
    """Whether the fills cannot be true, or the ledger row knows of more of them."""
    if any(
        filled_by_order_day.get(order.key, 0) > order.quantity
        for order in orders.placed
    ):
        return True
    if ledger_order is None:
        return False
    executed = sum(filled_by_order_day.get(order.key, 0) for order in orders.placed)
    return ledger_order.filled_qty is None or ledger_order.filled_qty > executed


def _resolve_lineage(
    rows: Sequence[ChildSnapshot],
    orders: LineageOrders,
    ledger_order: LedgerOrder | None,
    filled_by_order_day: Mapping[OrderDay, int],
) -> tuple[WorkingOrder | None, tuple[Unresolved, ...]]:
    if not orders.identity_known:
        return None, _unresolved(rows, UnresolvedReason.ORDER_IDENTITY_AMBIGUOUS)
    if _fills_conflict(orders, ledger_order, filled_by_order_day):
        return None, _unresolved(rows, UnresolvedReason.FILL_EVIDENCE_CONFLICT)

    last = rows[-1]
    if last.action == "cancel" and last.state is ChildState.ACCEPTED:
        return None, ()
    if (
        ledger_order is not None
        and not orders.replacement_unseen
        and _ledger_proves_gone(ledger_order, rows)
    ):
        return None, ()
    current = orders.current
    if current is not None and 0 < current.quantity <= filled_by_order_day.get(
        current.key, 0
    ):
        # Only this order's own fills: its quantity is what was still open.
        return None, ()
    if (
        ledger_order is not None
        and ledger_order.lifecycle_state not in OPEN_LEDGER_STATES
    ):
        return None, _unresolved(rows, UnresolvedReason.LEDGER_STATE_UNRESOLVED)

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
            and _order_number(ledger_order.order_no)
        )
    ]
    if unknown:
        return None, _unresolved(unknown, UnresolvedReason.NO_LEDGER_EVIDENCE)

    working = _senders(rows)[-1]
    if working.limit_price_krw is None:
        raise ValueError("a journaled submit or amend has no price")
    return (
        WorkingOrder(
            key=working.idempotency_key,
            price=working.limit_price_krw,
            qty=_sent_quantity(working),
        ),
        (),
    )


def build_reconciliation(
    children: Sequence[ChildSnapshot], evidence: LedgerEvidence
) -> Reconciliation:
    """Fold the journal and the ledger evidence into E2's view of the intent."""
    lineages = _lineages(children)
    orders_of = {
        root: lineage_orders(rows, _single(evidence.orders_by_root.get(root, ())))
        for root, rows in lineages.items()
    }
    holders = Counter(
        order.key for orders in orders_of.values() for order in orders.placed
    )

    working: list[WorkingOrder] = []
    unresolved: list[Unresolved] = []
    filled_total = 0
    for root, rows in lineages.items():
        ledger_rows = evidence.orders_by_root.get(root, ())
        orders = orders_of[root]
        if len(ledger_rows) > 1:
            unresolved.extend(_unresolved(rows, UnresolvedReason.LEDGER_ROW_AMBIGUOUS))
            continue
        if any(holders[order.key] > 1 for order in orders.placed):
            # One order has one owner. An amend that kept its number, or two lineages
            # holding one, cannot split the order's fills between them.
            unresolved.extend(
                _unresolved(rows, UnresolvedReason.ORDER_IDENTITY_AMBIGUOUS)
            )
            continue
        order, lineage_unresolved = _resolve_lineage(
            rows, orders, _single(ledger_rows), evidence.filled_by_order_day
        )
        unresolved.extend(lineage_unresolved)
        if order is not None:
            working.append(order)
        filled_total += sum(
            evidence.filled_by_order_day.get(placed.key, 0) for placed in orders.placed
        )

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
        return Reconciliation(False, None, None, tuple(unresolved))
    return Reconciliation(True, working[0] if working else None, filled_total, ())


class LedgerEvidenceReader(Protocol):
    async def read(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> LedgerEvidence: ...


def _whole_shares(value: Any) -> int:
    """KR shares are whole. Anything else raises, and the route answers 503."""
    amount = Decimal(str(value))
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("a share quantity is not a whole number")
    return int(amount)


def order_days_of(
    lineages: Mapping[str, Sequence[ChildSnapshot]],
    orders_by_root: Mapping[str, Sequence[LedgerOrder]],
) -> set[OrderDay]:
    """Every order the intent's lineages placed."""
    return {
        order.key
        for root, rows in lineages.items()
        for order in lineage_orders(rows, _single(orders_by_root.get(root, ()))).placed
    }


def executed_by_order_day(
    rows: Iterable[tuple[str, Any, str, datetime]],
    order_days: Collection[OrderDay],
) -> dict[OrderDay, int]:
    """Executed quantity per order, from ``(id, qty, source, filled_at)``.

    This value can close an order, so a fill counts only for the order it belongs
    to. Otherwise an old fill under a reused number would report a live order as
    finished. Writers differ on leading zeros, so numbers compare normalized.
    """
    incremental: dict[OrderDay, Decimal] = {}
    cumulative: dict[OrderDay, Decimal] = {}
    for order_id, quantity, source, filled_at in rows:
        order = (_order_number(order_id), trade_day_kst(filled_at))
        if order not in order_days:
            continue
        amount = Decimal(str(quantity))
        if source == "websocket":
            incremental[order] = incremental.get(order, Decimal(0)) + amount
        else:
            cumulative[order] = max(cumulative.get(order, Decimal(0)), amount)
    return {
        order: _whole_shares(
            max(
                incremental.get(order, Decimal(0)),
                cumulative.get(order, Decimal(0)),
            )
        )
        for order in incremental.keys() | cumulative.keys()
    }


def _estimated_fill(lifecycle_state: str, detail: Any) -> int | None:
    """The reconciler's estimate. A row in ``fill`` without one is unknown, not zero."""
    raw = detail.get("attributed_fill_qty") if isinstance(detail, Mapping) else None
    if raw is None:
        return None if lifecycle_state == "fill" else 0
    try:
        estimate = _whole_shares(raw)
    except (InvalidOperation, ValueError):
        return None
    return estimate if estimate >= 0 else None


def _detail_flag(detail: Any) -> bool | None:
    if isinstance(detail, Mapping):
        flag = detail.get("broker_cancel_confirmed")
        if isinstance(flag, bool):
            return flag
    return None


class IntentFillsReader(Protocol):
    async def read_fills(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> list[dict[str, Any]]: ...


def intent_fill_orders(
    children: Sequence[ChildSnapshot], rows: Sequence[ExecutionLedger]
) -> list[dict[str, Any]]:
    """Project committed fills for journaled senders only, never inferred orders.

    Cancel rows repeat a target number; only submit/amend establish its day.
    Preserve the journal's spelling in the answer and count each row once.
    """
    names: dict[OrderDay, str] = {}
    for child in sorted(children, key=lambda item: item.child_id):
        if child.action in {"submit", "amend"} and child.broker_order_id:
            key = (
                _order_number(child.broker_order_id),
                trade_day_kst(child.created_at),
            )
            names.setdefault(key, child.broker_order_id)
    effective = [
        row
        for row in rows
        if row.quarantined_at is None
        and (_order_number(row.broker_order_id), trade_day_kst(row.filled_at)) in names
    ]
    effective, _ = supersede_provisional(effective)
    orders: dict[str, dict[str, Any]] = {
        name: {"broker_order_id": name, "cumulative_qty": 0, "fills": []}
        for name in names.values()
    }
    for row in sorted(effective, key=lambda item: (item.filled_at, item.id)):
        name = names[(_order_number(row.broker_order_id), trade_day_kst(row.filled_at))]
        qty = _whole_shares(row.filled_qty)
        orders[name]["cumulative_qty"] += qty
        orders[name]["fills"].append(
            {
                "fill_id": str(row.id),
                "side": row.side,
                "qty": qty,
                "price": format(row.filled_price, "f"),
                "filled_at": row.filled_at.isoformat(),
                "committed_at": row.created_at.isoformat(),
            }
        )
    return list(orders.values())


class SqlLedgerEvidenceReader:
    """Reads ledger evidence in a ``READ ONLY`` transaction. Calls no broker."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> LedgerEvidence:
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
                orders_by_root.setdefault(
                    root_by_correlation[str(row.correlation_id)], []
                ).append(
                    LedgerOrder(
                        order_no=row.order_no,
                        status=row.status,
                        lifecycle_state=row.lifecycle_state,
                        filled_qty=_estimated_fill(
                            row.lifecycle_state, row.last_reconcile_detail
                        ),
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

    async def read_fills(
        self, intent: IntentSnapshot, children: Sequence[ChildSnapshot]
    ) -> list[dict[str, Any]]:
        """Only committed, effective ledger rows for this journal's order IDs."""
        numbers = {
            _order_number(child.broker_order_id).lstrip("0")
            for child in children
            if child.action in {"submit", "amend"} and child.broker_order_id
        }
        if not numbers:
            return []
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = await session.scalars(
                select(ExecutionLedger)
                .options(
                    load_only(
                        ExecutionLedger.id,
                        ExecutionLedger.broker,
                        ExecutionLedger.account_mode,
                        ExecutionLedger.venue,
                        ExecutionLedger.instrument_type,
                        ExecutionLedger.symbol,
                        ExecutionLedger.side,
                        ExecutionLedger.currency,
                        ExecutionLedger.broker_order_id,
                        ExecutionLedger.source,
                        ExecutionLedger.filled_qty,
                        ExecutionLedger.filled_price,
                        ExecutionLedger.filled_at,
                        ExecutionLedger.created_at,
                        ExecutionLedger.quarantined_at,
                    )
                )
                .where(
                    ExecutionLedger.broker == "kis",
                    ExecutionLedger.account_mode == "mock",
                    ExecutionLedger.venue == intent.venue,
                    ExecutionLedger.instrument_type == "equity_kr",
                    ExecutionLedger.symbol == intent.symbol,
                    ExecutionLedger.side == intent.side,
                    ExecutionLedger.currency == "KRW",
                    func.ltrim(ExecutionLedger.broker_order_id, "0").in_(list(numbers)),
                    execution_ledger_in_effect(),
                )
            )
            orders = intent_fill_orders(children, rows.all())
            await session.rollback()
        return orders

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
                func.ltrim(ExecutionLedger.broker_order_id, "0").in_(
                    list({number.lstrip("0") for number, _ in order_days})
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
