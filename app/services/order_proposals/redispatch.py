"""Read-only fail-closed eligibility checks for manual approval redispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.approval_window import (
    ApprovalWindowDecision,
    WindowEvaluator,
    evaluate_approval_window,
    evaluate_approval_window_boundary,
)
from app.services.order_proposals.dispatch import approval_window_failure_code
from app.services.order_proposals.dispatch_contract import ApprovalDispatchState
from app.services.order_proposals.revalidation import (
    PlaceOrderFn,
    _default_place_order_fn,
    _norm,
    _proposal_client_order_id,
    _toss_proposal_client_order_id,
)

Clock = Callable[[], datetime]

_ELIGIBLE_RUNG_STATES = frozenset({"pending_approval", "needs_reconfirm"})
_REDISPATCHABLE_STATES = frozenset(
    {
        None,
        ApprovalDispatchState.FAILED.value,
        ApprovalDispatchState.PARTIAL_FAILED.value,
    }
)


@dataclass(frozen=True, slots=True)
class RedispatchValidation:
    eligible: bool
    failure_code: str | None
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "failure_code": self.failure_code,
            "detail": self.detail,
        }


def _blocked(failure_code: str, **detail: Any) -> RedispatchValidation:
    return RedispatchValidation(False, failure_code, detail)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


async def validate_proposal_redispatch(
    *,
    group: Any,
    rungs: list[Any],
    now: datetime,
    place_order_fn: PlaceOrderFn = _default_place_order_fn,
    window_evaluator: WindowEvaluator = evaluate_approval_window,
    now_fn: Clock | None = None,
) -> RedispatchValidation:
    """Re-check one proposal without DB or broker mutation.

    A redispatch is intentionally narrower than create: only unbounded-free
    limit ``place`` proposals can be re-sent. Fresh order previews rerun current
    holdings/price guards and must preserve the exact normalized price and
    quantity stored on every rung.
    """
    if group.lifecycle_state != "proposed":
        return _blocked(
            "redispatch_proposal_not_active",
            lifecycle_state=group.lifecycle_state,
        )
    if (group.action or "place") != "place":
        return _blocked(
            "redispatch_action_not_supported",
            action=group.action or "place",
        )
    if group.order_type != "limit":
        return _blocked(
            "redispatch_market_order_unbounded",
            order_type=group.order_type,
        )
    if group.approval_nonce_used_at is not None:
        return _blocked("redispatch_approval_already_acted")
    if group.approval_dispatch_state == ApprovalDispatchState.PENDING.value:
        return _blocked("redispatch_dispatch_pending")
    if group.approval_dispatch_state == ApprovalDispatchState.SENT_CURRENT.value:
        return _blocked("redispatch_already_sent")
    if group.approval_dispatch_state not in _REDISPATCHABLE_STATES:
        return _blocked(
            "redispatch_dispatch_state_not_eligible",
            approval_dispatch_state=group.approval_dispatch_state,
        )
    if group.approval_dispatch_published_at is not None:
        return _blocked(
            "redispatch_publication_already_recorded",
            approval_dispatch_published_at=group.approval_dispatch_published_at.isoformat(),
        )
    if group.approval_nonce is not None:
        return _blocked("redispatch_active_nonce_present")
    if not rungs or any(rung.state not in _ELIGIBLE_RUNG_STATES for rung in rungs):
        return _blocked(
            "redispatch_rung_state_not_eligible",
            rung_states=[rung.state for rung in rungs],
        )

    clock = now_fn or (lambda: now)
    window: ApprovalWindowDecision = await evaluate_approval_window_boundary(
        group,
        window_evaluator=window_evaluator,
        now_fn=clock,
        require_policy_stamp=False,
    )
    if not window.allowed:
        return _blocked(
            approval_window_failure_code(window),
            approval_window=window.to_dict(),
        )

    previews: list[dict[str, Any]] = []
    for rung in rungs:
        proposal_client_order_id = (
            _toss_proposal_client_order_id(group.proposal_id, rung.rung_index)
            if group.account_mode == "toss_live"
            else _proposal_client_order_id(group.proposal_id, rung.rung_index)
            if group.account_mode == "upbit"
            else None
        )
        try:
            preview = await place_order_fn(
                dry_run=True,
                account_mode=group.account_mode,
                symbol=group.symbol,
                side=rung.side,
                market=group.market,
                order_type=group.order_type,
                quantity=rung.quantity,
                price=rung.limit_price,
                thesis=group.thesis,
                strategy=group.strategy,
                exit_intent=group.exit_intent,
                exit_reason=group.exit_reason,
                retrospective_id=group.retrospective_id,
                approval_issue_id=group.approval_issue_id,
                reason=f"order_proposal redispatch validation (rung {rung.rung_index})",
                rung=rung.rung_index,
                **(
                    {"proposal_client_order_id": proposal_client_order_id}
                    if proposal_client_order_id is not None
                    else {}
                ),
            )
        except Exception as exc:  # noqa: BLE001 - preview is read-only
            return _blocked(
                "redispatch_preview_exception",
                rung_index=rung.rung_index,
                exception_type=type(exc).__name__,
            )
        if not isinstance(preview, dict) or preview.get("success") is not True:
            return _blocked(
                "redispatch_preview_blocked",
                rung_index=rung.rung_index,
                error=(preview.get("error") if isinstance(preview, dict) else None),
                error_code=(
                    preview.get("error_code") if isinstance(preview, dict) else None
                ),
            )

        expected_price = _norm(rung.limit_price)
        preview_price = _norm(preview.get("price"))
        if preview_price != expected_price:
            return _blocked(
                "redispatch_price_changed",
                rung_index=rung.rung_index,
                expected_price=expected_price,
                preview_price=preview_price,
            )
        expected_quantity = _norm(rung.quantity)
        preview_quantity = _norm(preview.get("quantity"))
        if preview_quantity != expected_quantity:
            return _blocked(
                "redispatch_quantity_changed",
                rung_index=rung.rung_index,
                expected_quantity=expected_quantity,
                preview_quantity=preview_quantity,
            )

        current_price = _decimal(preview.get("current_price"))
        limit_price = _decimal(rung.limit_price)
        if current_price is None or current_price <= 0 or limit_price is None:
            return _blocked(
                "redispatch_current_price_missing",
                rung_index=rung.rung_index,
            )
        crossed_market = (rung.side == "buy" and limit_price > current_price) or (
            rung.side == "sell" and limit_price < current_price
        )
        if crossed_market:
            return _blocked(
                "redispatch_price_crossed_market",
                rung_index=rung.rung_index,
                side=rung.side,
                limit_price=_norm(limit_price),
                current_price=_norm(current_price),
            )
        previews.append(
            {
                "rung_index": rung.rung_index,
                "limit_price": expected_price,
                "quantity": expected_quantity,
                "current_price": _norm(current_price),
            }
        )

    return RedispatchValidation(
        True,
        None,
        {
            "approval_window": window.to_dict(),
            "rungs": previews,
        },
    )


__all__ = [
    "RedispatchValidation",
    "validate_proposal_redispatch",
]
