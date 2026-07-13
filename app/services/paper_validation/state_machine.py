"""Pure deterministic ROB-848 paper-validation state graph."""

from __future__ import annotations

from app.services.paper_validation.contracts import (
    TransitionDecision,
    ValidationState,
)

_LEGAL_TRANSITIONS: frozenset[tuple[ValidationState, ValidationState]] = frozenset(
    {
        (ValidationState.DRAFT, ValidationState.OFFLINE_ELIGIBLE),
        (ValidationState.OFFLINE_ELIGIBLE, ValidationState.SHADOW_SOAK),
        (ValidationState.SHADOW_SOAK, ValidationState.PAPER_ACTIVE),
        (ValidationState.PAPER_ACTIVE, ValidationState.PROMOTION_ELIGIBLE),
        (ValidationState.PROMOTION_ELIGIBLE, ValidationState.PROMOTED),
        (ValidationState.PROMOTION_ELIGIBLE, ValidationState.REJECTED),
        (ValidationState.PROMOTION_ELIGIBLE, ValidationState.ABORTED),
    }
)
_TERMINAL_STATES: frozenset[ValidationState] = frozenset(
    {
        ValidationState.PROMOTED,
        ValidationState.REJECTED,
        ValidationState.ABORTED,
    }
)
_ORDER_AUTHORIZABLE_STATES: frozenset[ValidationState] = frozenset(
    {ValidationState.PAPER_ACTIVE, ValidationState.PROMOTION_ELIGIBLE}
)


def decide_transition(
    prior: ValidationState | None,
    requested: ValidationState,
) -> TransitionDecision:
    if prior is None:
        if requested is ValidationState.DRAFT:
            return TransitionDecision(allowed=True)
        return TransitionDecision(allowed=False, reason_code="invalid_transition")
    if prior in _TERMINAL_STATES:
        return TransitionDecision(allowed=False, reason_code="terminal_state")
    if (prior, requested) in _LEGAL_TRANSITIONS:
        return TransitionDecision(allowed=True)
    return TransitionDecision(allowed=False, reason_code="invalid_transition")


def is_order_authorizable(state: ValidationState) -> bool:
    return state in _ORDER_AUTHORIZABLE_STATES


__all__ = ["decide_transition", "is_order_authorizable"]
