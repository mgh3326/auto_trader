from __future__ import annotations

import itertools

import pytest

from app.services.paper_validation.contracts import ValidationState
from app.services.paper_validation.state_machine import (
    decide_transition,
    is_order_authorizable,
)

LEGAL_EDGES = {
    (ValidationState.DRAFT, ValidationState.OFFLINE_ELIGIBLE),
    (ValidationState.OFFLINE_ELIGIBLE, ValidationState.SHADOW_SOAK),
    (ValidationState.SHADOW_SOAK, ValidationState.PAPER_ACTIVE),
    (ValidationState.PAPER_ACTIVE, ValidationState.PROMOTION_ELIGIBLE),
    (ValidationState.PROMOTION_ELIGIBLE, ValidationState.PROMOTED),
    (ValidationState.PROMOTION_ELIGIBLE, ValidationState.REJECTED),
    (ValidationState.PROMOTION_ELIGIBLE, ValidationState.ABORTED),
}
TERMINAL = {
    ValidationState.PROMOTED,
    ValidationState.REJECTED,
    ValidationState.ABORTED,
}


def test_initial_registration_is_only_null_to_draft() -> None:
    assert decide_transition(None, ValidationState.DRAFT).allowed is True
    for state in ValidationState:
        if state is ValidationState.DRAFT:
            continue
        decision = decide_transition(None, state)
        assert decision.allowed is False
        assert decision.reason_code == "invalid_transition"


@pytest.mark.parametrize(
    ("prior", "requested"),
    itertools.product(ValidationState, repeat=2),
)
def test_complete_transition_matrix(
    prior: ValidationState, requested: ValidationState
) -> None:
    decision = decide_transition(prior, requested)
    if (prior, requested) in LEGAL_EDGES:
        assert decision.allowed is True
        assert decision.reason_code is None
    elif prior in TERMINAL:
        assert decision.allowed is False
        assert decision.reason_code == "terminal_state"
    else:
        assert decision.allowed is False
        assert decision.reason_code == "invalid_transition"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            state,
            state in {ValidationState.PAPER_ACTIVE, ValidationState.PROMOTION_ELIGIBLE},
        )
        for state in ValidationState
    ],
)
def test_order_authorization_state_is_closed(
    state: ValidationState, expected: bool
) -> None:
    assert is_order_authorizable(state) is expected
