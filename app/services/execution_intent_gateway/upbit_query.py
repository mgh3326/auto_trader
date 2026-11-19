"""hk 5441 — reconcile an ``upbit_live`` intent from its journal and evidence.

The Upbit counterpart of ``query.build_reconciliation``. Read only, pure.

An Upbit order is one uuid for life: there is no amend and no number reuse, so
the lineage rules of the kis_mock query do not apply. What stays the same is the
rule that matters: an unknown outcome stays unknown without positive evidence,
and then ``view`` is ``null`` so the executor keeps holding (design §1.5).

* A child that was refused or proven not sent was never an order. It is left
  out of the view and stays in ``children`` for audit (S4), so a retry after a
  lost answer can never see a ``not_sent`` submit as a working order.
* A ``shadowed`` submit is a virtual fill in full (§3.6) and never working.
* A live order is closed by its evidence row (final broker state), by a cancel
  the gateway recorded as accepted, or by ledger fills that reach its quantity.
* ``filled_qty`` counts only in-effect ``execution_ledger`` rows that carry the
  order's uuid. It is as complete as that feed and no more.

Quantities are exact decimal strings (B20, B34).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.execution_intent_gateway.decisions import (
    NEVER_SENT_STATES,
    ChildSnapshot,
    ChildState,
)
from app.services.execution_intent_gateway.query import (
    Unresolved,
    UnresolvedReason,
    child_json,
)
from app.services.strategy_sleeves.intent_orders import IntentChildOrder

ZERO = Decimal("0")
_UNKNOWN = frozenset({ChildState.SENDING, ChildState.UNCERTAIN})


def exact(value: Decimal) -> str:
    """A decimal as a plain string: no exponent, no trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True, slots=True)
class UpbitWorkingOrder:
    key: str
    price: int
    qty: Decimal


@dataclass(frozen=True, slots=True)
class UpbitReconciliation:
    reconciled: bool
    order: UpbitWorkingOrder | None
    filled_qty: Decimal | None
    unresolved: tuple[Unresolved, ...]


def _unresolved(child: ChildSnapshot, reason: UnresolvedReason) -> Unresolved:
    return Unresolved(child.idempotency_key, child.state.value, reason)


def build_upbit_reconciliation(
    children: Sequence[ChildSnapshot],
    orders: Sequence[IntentChildOrder],
    filled_by_uuid: Mapping[str, Decimal],
) -> UpbitReconciliation:
    """Fold the journal, the order evidence and the ledger fills into E2's view.

    ``orders`` are this intent's orders from ``intent_orders``; ``filled_by_uuid``
    is the ledger's fill total per order uuid.
    """
    by_key = {child.idempotency_key: child for child in children}
    order_by_key = {order.idempotency_key: order for order in orders}
    cancels: dict[str, list[ChildSnapshot]] = {}
    for child in children:
        if (
            child.action == "cancel"
            and child.state not in NEVER_SENT_STATES
            and child.target_idempotency_key is not None
        ):
            cancels.setdefault(child.target_idempotency_key, []).append(child)

    working: list[UpbitWorkingOrder] = []
    unresolved: list[Unresolved] = []
    filled_total = ZERO
    for child in sorted(children, key=lambda row: row.child_id):
        if child.action != "submit" or child.state in NEVER_SENT_STATES:
            continue
        order = order_by_key.get(child.idempotency_key)
        if order is None or child.quantity_decimal is None:
            unresolved.append(_unresolved(child, UnresolvedReason.NO_LEDGER_EVIDENCE))
            continue
        if order.is_shadow:
            filled_total += order.quantity
            continue
        uuid = order.order_uuid
        ledger_filled = filled_by_uuid.get(uuid, ZERO) if uuid else ZERO
        if ledger_filled > order.quantity:
            unresolved.append(
                _unresolved(child, UnresolvedReason.FILL_EVIDENCE_CONFLICT)
            )
            continue
        filled_total += ledger_filled
        if order.resolved or ledger_filled >= order.quantity:
            continue
        own_cancels = cancels.get(child.idempotency_key, [])
        if any(cancel.state is ChildState.ACCEPTED for cancel in own_cancels):
            # The gateway recorded the broker taking the cancel (E1 rule).
            continue
        if child.state in _UNKNOWN or uuid is None:
            unresolved.append(_unresolved(child, UnresolvedReason.NO_LEDGER_EVIDENCE))
            continue
        pending = [cancel for cancel in own_cancels if cancel.state in _UNKNOWN]
        if pending:
            # B30: an unknown cancel is settled only by the order's own evidence.
            unresolved.extend(
                _unresolved(cancel, UnresolvedReason.NO_LEDGER_EVIDENCE)
                for cancel in pending
            )
            continue
        if child.limit_price_krw is None:
            raise ValueError("a journaled upbit submit has no price")
        working.append(
            UpbitWorkingOrder(
                key=child.idempotency_key,
                price=child.limit_price_krw,
                qty=child.quantity_decimal,
            )
        )

    if len(working) > 1:
        unresolved.extend(
            _unresolved(by_key[order.key], UnresolvedReason.MULTIPLE_WORKING_ORDERS)
            for order in working
        )
    if unresolved:
        return UpbitReconciliation(False, None, None, tuple(unresolved))
    return UpbitReconciliation(True, working[0] if working else None, filled_total, ())


def upbit_child_json(child: ChildSnapshot) -> dict[str, Any]:
    """One journal row. ``quantity_decimal`` replaces the integer ``quantity``."""
    row = child_json(child)
    row["quantity_decimal"] = (
        None if child.quantity_decimal is None else exact(child.quantity_decimal)
    )
    return row


def upbit_reconciliation_json(
    intent_id: UUID,
    children: Sequence[ChildSnapshot],
    reconciliation: UpbitReconciliation,
) -> dict[str, Any]:
    """The query answer for a v2 intent: the v1 shape with decimal strings."""
    view: dict[str, Any] | None = None
    if reconciliation.reconciled and reconciliation.filled_qty is not None:
        view = {"filled_qty": exact(reconciliation.filled_qty)}
        order = reconciliation.order
        if order is not None:
            view["order"] = {
                "key": order.key,
                "price": order.price,
                "qty": exact(order.qty),
            }
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
        "children": [upbit_child_json(child) for child in children],
    }


def upbit_fill_orders(
    orders: Sequence[IntentChildOrder], rows: Sequence[Any]
) -> list[dict[str, Any]]:
    """Committed fills per order uuid, for ``/fills`` (design §1.5, B34).

    ``rows`` are in-effect ledger rows already limited to these uuids, this
    account, side and currency. A websocket row an authoritative row covers is
    dropped by the same ROB-1379 rule every other reader uses. A shadow order
    has no ledger row and is not listed.
    """
    from app.services.execution_ledger.supersede import supersede_provisional

    listed: dict[str, dict[str, Any]] = {}
    for order in sorted(orders, key=lambda item: item.child_id):
        uuid = order.order_uuid
        if order.is_shadow or uuid is None:
            continue
        listed.setdefault(
            uuid, {"broker_order_id": uuid, "cumulative_qty": ZERO, "fills": []}
        )
    effective = [
        row
        for row in rows
        if row.quarantined_at is None and str(row.broker_order_id) in listed
    ]
    effective, _ = supersede_provisional(effective)
    for row in sorted(effective, key=lambda item: (item.filled_at, item.id)):
        entry = listed[str(row.broker_order_id)]
        quantity = Decimal(str(row.filled_qty))
        entry["cumulative_qty"] += quantity
        entry["fills"].append(
            {
                "fill_id": str(row.id),
                "side": row.side,
                "qty": exact(quantity),
                "price": format(Decimal(str(row.filled_price)), "f"),
                "filled_at": row.filled_at.isoformat(),
                "committed_at": row.created_at.isoformat(),
            }
        )
    for entry in listed.values():
        entry["cumulative_qty"] = exact(entry["cumulative_qty"])
    return list(listed.values())


def filled_by_uuid(fill_orders: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    return {
        str(entry["broker_order_id"]): Decimal(str(entry["cumulative_qty"]))
        for entry in fill_orders
    }
