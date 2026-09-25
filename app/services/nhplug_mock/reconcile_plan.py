"""Pure reconcile planning for the NHPLUG mock ledger.

Inputs are ledger row views plus two independently fetched broker listings:
``all_listing`` (every order today) and ``open_listing`` (open-only scope).
Output is one :class:`ReconcileUpdate` per row, or none.

Invariants (the Kiwoom ``kt00009`` lesson):

* An incomplete or error-shaped listing never closes anything; the row is
  marked ``unknown`` with the reason and its status is untouched.
* An order missing from the all-orders listing is ``unknown``, never closed.
* A terminal state (filled/cancelled/modified/rejected/confirmed) needs the
  all-orders row to say so **and** a complete open-only listing that does not
  contain the order.  If the open-only scope still lists it, the row becomes
  ``source_disagreement`` and keeps its status.
* An open state needs the all-orders row **and** the open-only scope to agree.
* Fill quantities come only from a listing row (evidence-first), and any
  non-zero fill must also appear with the same quantity in the independent
  filled-only scope (``ost_cns_dit=1``); otherwise the row stays ``unknown``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.services.brokers.nhplug.order_evidence import (
    TERMINAL_ORDER_STATUSES,
    OrderListing,
    OrderRow,
    derive_order_status,
)
from app.services.nhplug_mock.ledger_service import ReconcileUpdate

RECONCILABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "submitting",
        "accepted",
        "acceptance_uncertain",
        "open",
        "partially_filled",
        "rejected",
    }
)
_UNBOUND_STATUSES: Final[frozenset[str]] = frozenset(
    {"submitting", "acceptance_uncertain", "rejected"}
)


@dataclass(frozen=True, slots=True)
class LedgerRowView:
    id: int
    operation_kind: str
    status: str
    symbol: str
    side: str | None
    quantity: int | None
    price: Decimal | None
    broker_order_id: str | None
    original_order_id: str | None


@dataclass(frozen=True, slots=True)
class PlannedUpdate:
    row_id: int
    update: ReconcileUpdate


def _order_no(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    number = int(value)
    return number if number > 0 else None


def _unknown(reason: str, **extra: object) -> ReconcileUpdate:
    return ReconcileUpdate(reconcile_state="unknown", note={"reason": reason, **extra})


def _fill_fields(row: OrderRow) -> dict[str, object]:
    return {
        "filled_qty": row.filled_qty,
        "avg_fill_price": row.avg_fill_price if row.filled_qty > 0 else None,
        "open_qty": row.open_qty,
        "cancelled_qty": row.cancelled_qty,
        "evidence": row.evidence(),
    }


def _fill_confirmed(
    broker_row: OrderRow, filled_listing: OrderListing | None
) -> str | None:
    """Return a reason when a non-zero fill lacks filled-scope confirmation."""

    if broker_row.filled_qty == 0:
        return None
    if filled_listing is None or not filled_listing.complete:
        reason = None if filled_listing is None else filled_listing.reason
        return f"filled_scope_incomplete:{reason}"
    confirming = filled_listing.find(broker_row.order_no)
    if confirming is None or confirming.filled_qty != broker_row.filled_qty:
        return "fill_not_confirmed_by_filled_scope"
    return None


def _plan_bound_order(
    view: LedgerRowView,
    order_no: int,
    *,
    all_listing: OrderListing,
    open_listing: OrderListing,
    filled_listing: OrderListing | None,
) -> ReconcileUpdate:
    broker_row = all_listing.find(order_no)
    if broker_row is None:
        return _unknown("order_missing_from_all_orders_listing")
    if broker_row.symbol != view.symbol or (
        view.side is not None
        and broker_row.side is not None
        and broker_row.side != view.side
    ):
        return ReconcileUpdate(
            reconcile_state="verified",
            status="anomaly",
            evidence=broker_row.evidence(),
            note={"reason": "broker_row_does_not_match_ledger_order"},
            requires_manual_review=True,
            manual_review_reason="broker_row_does_not_match_ledger_order",
        )
    derived = derive_order_status(broker_row)
    if derived == "unknown":
        return _unknown(
            "broker_quantities_inconsistent", evidence=broker_row.evidence()
        )
    if not open_listing.complete:
        return _unknown(f"open_scope_incomplete:{open_listing.reason}")
    in_open_scope = open_listing.find(order_no) is not None
    if derived in TERMINAL_ORDER_STATUSES:
        if in_open_scope:
            return ReconcileUpdate(
                reconcile_state="source_disagreement",
                note={
                    "reason": "all_scope_terminal_but_open_scope_lists_order",
                    "derived": derived,
                },
            )
    elif not in_open_scope:
        return ReconcileUpdate(
            reconcile_state="source_disagreement",
            note={
                "reason": "all_scope_open_but_open_scope_omits_order",
                "derived": derived,
            },
        )
    if view.status == "rejected":
        # A broker-rejected ack whose number later appears is an anomaly.
        return ReconcileUpdate(
            reconcile_state="verified",
            status="anomaly",
            evidence=broker_row.evidence(),
            note={"reason": "rejected_order_present_in_listing"},
            requires_manual_review=True,
            manual_review_reason="rejected_order_present_in_listing",
        )
    if view.status == "partially_filled" and derived == "open":
        return _unknown(
            "broker_open_quantity_regressed", evidence=broker_row.evidence()
        )
    if (fill_gap := _fill_confirmed(broker_row, filled_listing)) is not None:
        return _unknown(fill_gap, evidence=broker_row.evidence())
    return ReconcileUpdate(
        reconcile_state="verified",
        status=derived,
        note={"reason": "broker_listing_verified", "derived": derived},
        **_fill_fields(broker_row),  # type: ignore[arg-type]
    )


def _plan_cancel(
    view: LedgerRowView,
    *,
    all_listing: OrderListing,
    open_listing: OrderListing,
    claimed: set[int],
) -> ReconcileUpdate | None:
    original_no = _order_no(view.original_order_id)
    if original_no is None:
        return _unknown("cancel_without_original_order_number")
    original = all_listing.find(original_no)
    if original is None:
        return _unknown("original_order_missing_from_all_orders_listing")
    bind: str | None = None
    if view.broker_order_id is None:
        if view.status == "rejected":
            return None
        candidates = [
            row
            for row in all_listing.rows
            if row.original_order_no == original_no
            and row.order_no not in claimed
            and row.correction_kind is not None
            and "취소" in row.correction_kind
        ]
        if len(candidates) != 1:
            return _unknown(
                "cancel_order_number_not_uniquely_matched",
                candidate_count=len(candidates),
            )
        bind = str(candidates[0].order_no)
    if not open_listing.complete:
        return _unknown(f"open_scope_incomplete:{open_listing.reason}")
    if original.open_qty > 0 or open_listing.find(original_no) is not None:
        return ReconcileUpdate(
            reconcile_state="pending",
            broker_order_id=bind,
            note={"reason": "original_order_still_open"},
        )
    if (original.cancelled_qty or 0) <= 0:
        return ReconcileUpdate(
            reconcile_state="verified",
            status="anomaly",
            broker_order_id=bind,
            evidence=original.evidence(),
            note={"reason": "original_closed_without_cancelled_quantity"},
            requires_manual_review=True,
            manual_review_reason="cancel_not_reflected_in_original_order",
        )
    if view.status == "rejected":
        return None
    return ReconcileUpdate(
        reconcile_state="verified",
        status="confirmed",
        broker_order_id=bind,
        evidence=original.evidence(),
        cancelled_qty=original.cancelled_qty,
        note={"reason": "original_order_cancel_reflected"},
    )


def _plan_unbound_order(
    view: LedgerRowView, *, all_listing: OrderListing, claimed: set[int]
) -> ReconcileUpdate | None:
    original_no = _order_no(view.original_order_id)
    candidates = [
        row
        for row in all_listing.rows
        if row.order_no not in claimed
        and row.symbol == view.symbol
        and view.quantity is not None
        and row.order_qty == view.quantity
        and view.price is not None
        and row.order_price == view.price
        and (view.side is None or row.side is None or row.side == view.side)
        and (view.operation_kind != "modify" or row.original_order_no == original_no)
    ]
    if view.status == "rejected":
        if len(candidates) != 1:
            return None
        return ReconcileUpdate(
            reconcile_state="verified",
            status="anomaly",
            evidence=candidates[0].evidence(),
            note={"reason": "rejected_order_matches_unclaimed_broker_order"},
            requires_manual_review=True,
            manual_review_reason="rejected_order_matches_unclaimed_broker_order",
        )
    if not candidates:
        return _unknown("no_matching_broker_order_found")
    if len(candidates) > 1:
        return _unknown(
            "ambiguous_matching_broker_orders", candidate_count=len(candidates)
        )
    match = candidates[0]
    return ReconcileUpdate(
        reconcile_state="pending",
        status="accepted",
        broker_order_id=str(match.order_no),
        note={
            "reason": "bound_by_unique_attribute_match",
            "evidence": match.evidence(),
        },
        requires_manual_review=True,
        manual_review_reason="broker_order_number_bound_by_attribute_match",
    )


def plan_reconcile(
    rows: Sequence[LedgerRowView],
    *,
    all_listing: OrderListing,
    open_listing: OrderListing,
    filled_listing: OrderListing | None = None,
    all_claimed_order_ids: Iterable[str | None] = (),
) -> list[PlannedUpdate]:
    """Plan updates for every reconcilable row; pure and deterministic."""

    claimed = {
        number
        for number in (_order_no(value) for value in all_claimed_order_ids)
        if number is not None
    }
    planned: list[PlannedUpdate] = []
    for view in rows:
        if view.status not in RECONCILABLE_STATUSES:
            continue
        if not all_listing.complete:
            if view.status == "rejected":
                continue
            planned.append(
                PlannedUpdate(
                    view.id, _unknown(f"all_scope_incomplete:{all_listing.reason}")
                )
            )
            continue
        update: ReconcileUpdate | None
        if view.operation_kind == "cancel":
            update = _plan_cancel(
                view,
                all_listing=all_listing,
                open_listing=open_listing,
                claimed=claimed,
            )
        else:
            order_no = _order_no(view.broker_order_id)
            if order_no is not None:
                update = _plan_bound_order(
                    view,
                    order_no,
                    all_listing=all_listing,
                    open_listing=open_listing,
                    filled_listing=filled_listing,
                )
            elif view.status in _UNBOUND_STATUSES:
                update = _plan_unbound_order(
                    view, all_listing=all_listing, claimed=claimed
                )
            else:
                update = _unknown("live_status_without_broker_order_number")
        if update is not None:
            planned.append(PlannedUpdate(view.id, update))
            if update.broker_order_id is not None:
                bound = _order_no(update.broker_order_id)
                if bound is not None:
                    claimed.add(bound)
    return planned
