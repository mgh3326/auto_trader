"""hk 6066 (6033 K2) — a read-only ``exposure`` block for ``GET /intents/<id>``.

Pure functions. They only *report* what the cap rules already count; they never
decide anything and never feed back into :func:`decisions.decide` or
:func:`upbit_decisions.decide_upbit_child`.

* The per-order high-water figures are :func:`decisions._exposure_by_order`
  (kis_mock) and the per-submit figures :func:`upbit_decisions.sent_exposure`
  counts (upbit_live), so this block can never disagree with the cap check.
* **D-4 holds: a confirmed cancel releases nothing.** ``release`` is always
  ``"none"``. ``cancel_evidence`` is an observation (broker terminal state plus a
  ledger that shows no fill), the raw material of a later, separately decided,
  release (K9). Nothing here frees a cap.
* Unknown stays unknown. Evidence that is missing, ambiguous or conflicting
  yields ``confirmed_cancel_no_fill: false`` with the reason left visible, never
  a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from app.services.execution_intent_gateway.decisions import (
    NEVER_SENT_STATES,
    ChildSnapshot,
    IntentSnapshot,
    _exposure_by_order,
)
from app.services.execution_intent_gateway.query import (
    LedgerEvidence,
    LedgerOrder,
    _lineages,
    _single,
    lineage_orders,
)
from app.services.execution_intent_gateway.upbit_decisions import (
    child_notional,
    sent_exposure,
)
from app.services.execution_intent_gateway.upbit_query import exact
from app.services.kis_mock_runner.envelope import LOCKED_ENVELOPE
from app.services.strategy_sleeves.intent_orders import IntentChildOrder

#: A confirmed cancel frees no cap (D-4). Not a flag: there is nothing to turn on.
RELEASE: Final = "none"
BASIS: Final = "journal_high_water"
ZERO: Final = Decimal("0")


def _evidence(
    *,
    broker_terminal: str | None,
    ledger_fill_qty: str | None,
    confirmed: bool,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "broker_terminal": broker_terminal,
        "ledger_fill_qty": ledger_fill_qty,
        "confirmed_cancel_no_fill": confirmed,
        "reason": reason,
    }


def _kis_cancel_evidence(
    rows: Sequence[ChildSnapshot],
    ledger_rows: Sequence[LedgerOrder],
    evidence: LedgerEvidence,
    *,
    key_is_shared: bool,
) -> dict[str, Any]:
    if len(ledger_rows) > 1:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=None,
            confirmed=False,
            reason="ledger_row_ambiguous",
        )
    ledger_order = _single(ledger_rows)
    orders = lineage_orders(rows, ledger_order)
    if not orders.identity_known or key_is_shared:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=None,
            confirmed=False,
            reason="order_identity_ambiguous",
        )
    if ledger_order is None:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=None,
            confirmed=False,
            reason="no_ledger_evidence",
        )
    executed = sum(evidence.filled_by_order_day.get(o.key, 0) for o in orders.placed)
    terminal: str | None = None
    # The broker's terminal word, as the ledger proves it. A soft cancel (no broker
    # confirmation) is not terminal: the order may still be live.
    if ledger_order.lifecycle_state == "cancelled":
        terminal = "cancelled" if ledger_order.broker_cancel_confirmed else None
    elif ledger_order.lifecycle_state == "expired":
        terminal = "expired"
    if terminal is None:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=str(executed),
            confirmed=False,
            reason="broker_terminal_unproven",
        )
    if orders.replacement_unseen:
        # An amend with an unknown outcome may have placed an order the ledger
        # does not speak for.
        return _evidence(
            broker_terminal=terminal,
            ledger_fill_qty=str(executed),
            confirmed=False,
            reason="replacement_unseen",
        )
    if ledger_order.filled_qty is None or ledger_order.filled_qty > executed:
        return _evidence(
            broker_terminal=terminal,
            ledger_fill_qty=str(executed),
            confirmed=False,
            reason="fill_evidence_conflict",
        )
    if executed > 0:
        return _evidence(
            broker_terminal=terminal,
            ledger_fill_qty=str(executed),
            confirmed=False,
            reason="filled",
        )
    return _evidence(
        broker_terminal=terminal,
        ledger_fill_qty="0",
        confirmed=terminal == "cancelled",
        reason=None if terminal == "cancelled" else "expired_not_cancel",
    )


def build_kis_exposure(
    intent: IntentSnapshot,
    children: Sequence[ChildSnapshot],
    evidence: LedgerEvidence,
) -> dict[str, Any]:
    """The exposure of a ``kis_mock`` intent. Quantities are whole shares."""
    by_root = _exposure_by_order(children)
    lineages = _lineages(children)
    orders_of = {
        root: lineage_orders(rows, _single(evidence.orders_by_root.get(root, ())))
        for root, rows in lineages.items()
    }
    holders: dict[Any, int] = {}
    for orders in orders_of.values():
        for placed in orders.placed:
            holders[placed.key] = holders.get(placed.key, 0) + 1

    listed: list[dict[str, Any]] = []
    for root, (quantity, notional) in by_root.items():
        orders = orders_of.get(root)
        shared = orders is not None and any(
            holders[placed.key] > 1 for placed in orders.placed
        )
        listed.append(
            {
                "root_idempotency_key": root,
                "high_water_quantity": quantity,
                "high_water_notional_krw": notional,
                "cancel_evidence": _kis_cancel_evidence(
                    lineages.get(root, ()),
                    evidence.orders_by_root.get(root, ()),
                    evidence,
                    key_is_shared=shared,
                ),
            }
        )
    total_quantity = sum(value[0] for value in by_root.values())
    total_notional = sum(value[1] for value in by_root.values())
    return {
        "basis": BASIS,
        "release": RELEASE,
        "cap": {
            "max_quantity": intent.max_quantity,
            "max_notional_krw": intent.max_notional_krw,
            "max_single_notional_krw": int(LOCKED_ENVELOPE.max_single_notional_krw),
        },
        "counted": {"quantity": total_quantity, "notional_krw": total_notional},
        "remaining": {
            "quantity": max(0, intent.max_quantity - total_quantity),
            "notional_krw": max(0, intent.max_notional_krw - total_notional),
        },
        "orders": listed,
    }


def _upbit_cancel_evidence(
    order: IntentChildOrder | None, ledger_filled: Decimal
) -> dict[str, Any]:
    if order is None:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=None,
            confirmed=False,
            reason="no_order_evidence",
        )
    if order.is_shadow:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=None,
            confirmed=False,
            reason="shadow_order",
        )
    found = order.evidence
    if found is None or order.order_uuid is None:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=None,
            confirmed=False,
            reason="no_order_evidence",
        )
    terminal = found.broker_state if order.resolved else None
    if terminal is None:
        return _evidence(
            broker_terminal=None,
            ledger_fill_qty=exact(ledger_filled),
            confirmed=False,
            reason="broker_terminal_unproven",
        )
    # The order is closed at the broker. It counts as a cancel with no fill only
    # when neither the broker's executed volume nor the ledger saw a trade.
    no_fill = found.executed_volume == ZERO and ledger_filled == ZERO
    confirmed = terminal == "cancel" and no_fill
    reason: str | None = None
    if not confirmed:
        reason = "filled" if not no_fill else "ended_not_cancel"
    return _evidence(
        broker_terminal=terminal,
        ledger_fill_qty=exact(ledger_filled),
        confirmed=confirmed,
        reason=reason,
    )


def build_upbit_exposure(
    intent: IntentSnapshot,
    children: Sequence[ChildSnapshot],
    orders: Sequence[IntentChildOrder],
    filled_by_uuid: Mapping[str, Decimal],
) -> dict[str, Any]:
    """The exposure of an ``upbit_live`` intent. Quantities are exact decimals.

    Every submit not refused and not proven unsent counts in full (E1 "How caps
    count"), cancelled ones included. A sell has a quantity cap only (B7), so its
    notional cap and remainder are ``null``.
    """
    order_by_key = {order.idempotency_key: order for order in orders}
    listed: list[dict[str, Any]] = []
    for child in sorted(children, key=lambda row: row.child_id):
        if (
            child.state in NEVER_SENT_STATES
            or child.action != "submit"
            or child.quantity_decimal is None
            or child.limit_price_krw is None
        ):
            continue
        order = order_by_key.get(child.idempotency_key)
        uuid = None if order is None else order.order_uuid
        ledger_filled = filled_by_uuid.get(uuid, ZERO) if uuid else ZERO
        listed.append(
            {
                "root_idempotency_key": child.idempotency_key,
                "high_water_quantity": exact(child.quantity_decimal),
                "high_water_notional_krw": exact(
                    child_notional(child.quantity_decimal, child.limit_price_krw)
                ),
                "cancel_evidence": _upbit_cancel_evidence(order, ledger_filled),
            }
        )
    total_quantity, total_notional = sent_exposure(children)
    max_quantity = intent.max_quantity_decimal
    notional_capped = intent.side == "buy"
    return {
        "basis": BASIS,
        "release": RELEASE,
        "cap": {
            "max_quantity": None if max_quantity is None else exact(max_quantity),
            "max_notional_krw": (
                str(intent.max_notional_krw) if notional_capped else None
            ),
        },
        "counted": {
            "quantity": exact(total_quantity),
            "notional_krw": exact(total_notional),
        },
        "remaining": {
            "quantity": (
                None
                if max_quantity is None
                else exact(max(ZERO, max_quantity - total_quantity))
            ),
            "notional_krw": (
                exact(max(ZERO, Decimal(intent.max_notional_krw) - total_notional))
                if notional_capped
                else None
            ),
        },
        "orders": listed,
    }


__all__ = ["BASIS", "RELEASE", "build_kis_exposure", "build_upbit_exposure"]
