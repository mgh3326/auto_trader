import pytest

from app.services.order_proposals import state_machine as sm
from app.services.order_proposals.errors import OrderProposalInvalidStateTransition


@pytest.mark.unit
def test_happy_path_allowed():
    for a, b in [
        ("pending_approval", "revalidating"),
        ("revalidating", "approved"),
        ("approved", "submitting"),
        ("submitting", "resting"),
        ("resting", "filled"),
    ]:
        sm.assert_rung_transition(a, b)  # no raise


@pytest.mark.unit
def test_confirmed_cancel_terminalizes_submitting():
    sm.assert_rung_transition("submitting", "cancelled")


@pytest.mark.unit
def test_accepted_is_not_filled():
    # A rung may only reach filled via ACKED/RESTING — never straight from submitting-approved.
    with pytest.raises(OrderProposalInvalidStateTransition):
        sm.assert_rung_transition("approved", "filled")


@pytest.mark.unit
def test_timeout_never_auto_voids():
    # submitting/acked/resting may go UNVERIFIED but not VOIDED_LOCAL_STALE.
    sm.assert_rung_transition("submitting", "unverified")
    with pytest.raises(OrderProposalInvalidStateTransition):
        sm.assert_rung_transition("submitting", "voided_local_stale")


@pytest.mark.unit
def test_local_stale_only_from_pending():
    sm.assert_rung_transition("pending_approval", "voided_local_stale")


@pytest.mark.unit
def test_needs_reconfirm_returns_to_pending():
    sm.assert_rung_transition("revalidating", "needs_reconfirm")
    sm.assert_rung_transition("needs_reconfirm", "pending_approval")


@pytest.mark.unit
def test_terminal_has_no_exits():
    for term in (
        "filled",
        "cancelled",
        "expired",
        "rejected",
        "voided",
        "voided_local_stale",
        "superseded",
    ):
        assert sm.is_terminal(term)
        with pytest.raises(OrderProposalInvalidStateTransition):
            sm.assert_rung_transition(term, "pending_approval")


@pytest.mark.parametrize(
    "state",
    ["draft", "pending_approval", "revalidating", "needs_reconfirm", "approved"],
)
def test_pre_submit_states_can_expire(state):
    sm.assert_rung_transition(state, "expired")


@pytest.mark.parametrize(
    "state",
    ["draft", "pending_approval", "revalidating", "needs_reconfirm", "approved"],
)
def test_pre_submit_states_can_void(state):
    sm.assert_rung_transition(state, "voided")
