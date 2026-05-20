"""ROB-274 — pure classifier: draft items + context → classified items.

Decision rules:

* watch/create   no matching active watch
* watch/keep     exactly one matching active watch with same condition
* watch/modify   exactly one matching active watch with changed condition
* watch/review   multiple matching active watches (ambiguous target)
* action/keep    pending broker order exists for same symbol/side and not stale
* action/review  pending broker order stale, or pending_orders snapshot missing
* action/modify, action/cancel  out-of-scope auto-classification in this PR —
                                callers can still produce these directly with
                                pre-filled fields; the classifier never
                                downgrades them.

The classifier never mutates broker or watch state. It only enriches
draft items with operation/target_ref/current_state/proposed_state/diff
plus a default ``apply_policy='requires_user_approval'`` for proposals
that reference existing state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.investment_reports import (
    IngestReportItem,
    TargetRefPayload,
)


@dataclass(slots=True)
class ClassifierContext:
    """Operational state inputs the classifier consults."""

    active_watches: list[dict[str, Any]] = field(default_factory=list)
    # None means the pending_orders snapshot was unavailable (collector
    # failed open). Empty list means the snapshot was fresh and reported
    # no open orders. The classifier MUST distinguish these.
    pending_orders: list[dict[str, Any]] | None = field(default_factory=list)


def classify_items(
    *, items: list[IngestReportItem], context: ClassifierContext
) -> list[IngestReportItem]:
    """Return new IngestReportItem instances with operation/etc. populated."""

    return [_classify_one(item, context) for item in items]


def _classify_one(
    item: IngestReportItem, context: ClassifierContext
) -> IngestReportItem:
    if item.operation is not None:
        # Caller pre-classified — pass through unchanged.
        return item
    if item.item_kind == "watch":
        return _classify_watch(item, context)
    if item.item_kind == "action":
        return _classify_action(item, context)
    # risk items are not auto-classified for proposal semantics; default
    # to operation=review when target_ref already set, else leave None.
    return item


def _classify_watch(
    item: IngestReportItem, context: ClassifierContext
) -> IngestReportItem:
    if item.symbol is None or item.watch_condition is None:
        return item.model_copy(update={"operation": "create"})

    candidates = [
        a
        for a in context.active_watches
        if a.get("symbol") == item.symbol
        and a.get("metric") == item.watch_condition.metric
    ]
    if not candidates:
        return item.model_copy(update={"operation": "create"})
    if len(candidates) > 1:
        return item.model_copy(
            update={
                "operation": "review",
                "target_ref": TargetRefPayload(
                    type="ambiguous",
                    candidates=[
                        {"id": str(c["alert_uuid"]), "threshold": str(c["threshold"])}
                        for c in candidates
                    ],
                ),
                "current_state": {
                    "metric": item.watch_condition.metric,
                    "active_alert_count": len(candidates),
                    "note": "multiple active alerts on same symbol/metric",
                },
                "rationale": item.rationale + " (다중 활성 와치 감지 — 수동 검토 필요)",
                "apply_policy": "requires_user_approval",
            }
        )
    alert = candidates[0]
    current_state = {
        "metric": alert.get("metric"),
        "operator": alert.get("operator"),
        "threshold": str(alert.get("threshold")),
        "action_mode": alert.get("action_mode"),
    }
    proposed_state = {
        "metric": item.watch_condition.metric,
        "operator": item.watch_condition.operator,
        "threshold": str(item.watch_condition.threshold),
        "action_mode": item.watch_condition.action_mode,
    }
    diff = _diff_states(current_state, proposed_state)
    target_ref = TargetRefPayload(
        type="investment_watch_alert",
        id=str(alert["alert_uuid"]),
        status=alert.get("status"),
    )
    if not diff:
        return item.model_copy(
            update={
                "operation": "keep",
                "target_ref": target_ref,
                "current_state": current_state,
                "apply_policy": "requires_user_approval",
            }
        )
    return item.model_copy(
        update={
            "operation": "modify",
            "target_ref": target_ref,
            "current_state": current_state,
            "proposed_state": proposed_state,
            "diff": diff,
            "apply_policy": "requires_user_approval",
        }
    )


def _classify_action(
    item: IngestReportItem, context: ClassifierContext
) -> IngestReportItem:
    if context.pending_orders is None:
        return item.model_copy(
            update={
                "operation": "review",
                "rationale": item.rationale + " (pending order 확인 불가)",
                "apply_policy": "requires_user_approval",
            }
        )
    if item.symbol is None or item.side is None:
        return item
    matching = [
        o
        for o in context.pending_orders
        if o.get("symbol") == item.symbol and o.get("side") == item.side
    ]
    if not matching:
        return item  # nothing to overlap with; caller's draft stands.
    if any(o.get("stale") for o in matching):
        # Stale pending order requires human review before placing fresh order.
        first = matching[0]
        return item.model_copy(
            update={
                "operation": "review",
                "target_ref": TargetRefPayload(**first["target_ref"]),
                "current_state": _order_to_state(first),
                "rationale": item.rationale + " (기존 미체결 주문 stale)",
                "apply_policy": "requires_user_approval",
            }
        )
    first = matching[0]
    return item.model_copy(
        update={
            "operation": "keep",
            "target_ref": TargetRefPayload(**first["target_ref"]),
            "current_state": _order_to_state(first),
            "apply_policy": "requires_user_approval",
        }
    )


def _diff_states(
    current: dict[str, Any], proposed: dict[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    keys = set(current) | set(proposed)
    for key in sorted(keys):
        a = current.get(key)
        b = proposed.get(key)
        if a != b:
            out.append({"field": key, "from": a, "to": b})
    return out


def _order_to_state(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": order.get("side"),
        "price": order.get("price"),
        "quantity": order.get("quantity"),
        "remaining_quantity": order.get("remaining_quantity"),
        "placed_at": order.get("placed_at"),
        "stale": order.get("stale"),
    }
