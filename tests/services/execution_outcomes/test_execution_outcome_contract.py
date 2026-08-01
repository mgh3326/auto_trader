from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from app.services.execution_outcomes import (
    BrokerAcceptance,
    EvidenceFinality,
    MutationCommand,
    MutationOperation,
    MutationOutcome,
    OutcomeNextAction,
    TerminalEvidence,
    TrackingState,
)


def _accepted_outcome(
    *,
    tracking: TrackingState = TrackingState.TRACKED,
    local_recorded: bool = False,
    reconcile_required: bool = False,
    evidence: TerminalEvidence = TerminalEvidence.NONE,
) -> MutationOutcome:
    return MutationOutcome(
        request_validated=True,
        mutation_sent=True,
        broker_acceptance=BrokerAcceptance.ACCEPTED,
        tracking=tracking,
        local_recorded=local_recorded,
        reconcile_required=reconcile_required,
        terminal_evidence=evidence,
    )


def test_command_consumes_rob1189_writer_binding_as_opaque_precondition() -> None:
    command = MutationCommand(
        command_id="command-1",
        designated_writer_ref="rob1189-binding/kiwoom-mock/kr-b1",
        account_mode="kiwoom_mock",
        operation=MutationOperation.PLACE,
        idempotency_key="client-order-1",
    )

    assert command.designated_writer_ref.startswith("rob1189-binding/")
    with pytest.raises(FrozenInstanceError):
        command.account_mode = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("command_id", ""),
        ("designated_writer_ref", "   "),
        ("account_mode", ""),
        ("idempotency_key", " "),
    ],
)
def test_command_rejects_blank_identity_fields(field_name: str, value: str) -> None:
    kwargs = {
        "command_id": "command-1",
        "designated_writer_ref": "rob1189-binding/1",
        "account_mode": "kis_live",
        "operation": MutationOperation.PLACE,
        "idempotency_key": "idempotency-1",
    }
    kwargs[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        MutationCommand(**kwargs)  # type: ignore[arg-type]


def test_success_is_not_an_outcome_axis_and_accepted_is_not_filled() -> None:
    outcome = _accepted_outcome()

    assert "success" not in {field.name for field in fields(MutationOutcome)}
    assert outcome.broker_acceptance is BrokerAcceptance.ACCEPTED
    assert outcome.terminal_evidence is TerminalEvidence.NONE
    assert outcome.evidence_finality is EvidenceFinality.NON_TERMINAL
    assert outcome.next_action is OutcomeNextAction.TRACK
    assert outcome.is_filled is False


def test_unknown_acceptance_requires_reconcile_and_never_authorizes_resend() -> None:
    outcome = MutationOutcome(
        request_validated=True,
        mutation_sent=True,
        broker_acceptance=BrokerAcceptance.UNKNOWN,
        tracking=TrackingState.UNKNOWN,
        local_recorded=False,
        reconcile_required=True,
        terminal_evidence=TerminalEvidence.NONE,
    )

    assert outcome.next_action is OutcomeNextAction.RECONCILE
    assert outcome.automatic_resend_allowed is False
    with pytest.raises(ValueError, match="unknown acceptance requires reconciliation"):
        MutationOutcome(
            request_validated=True,
            mutation_sent=True,
            broker_acceptance=BrokerAcceptance.UNKNOWN,
            tracking=TrackingState.UNKNOWN,
            local_recorded=False,
            reconcile_required=False,
            terminal_evidence=TerminalEvidence.NONE,
        )


def test_accepted_untracked_requires_reconciliation() -> None:
    outcome = _accepted_outcome(
        tracking=TrackingState.UNTRACKED,
        reconcile_required=True,
    )

    assert outcome.next_action is OutcomeNextAction.RECONCILE
    assert outcome.automatic_resend_allowed is False
    with pytest.raises(ValueError, match="accepted-untracked requires reconciliation"):
        _accepted_outcome(tracking=TrackingState.UNTRACKED)


def test_partial_fill_is_explicitly_non_terminal_and_keeps_tracking() -> None:
    outcome = _accepted_outcome(
        reconcile_required=True,
        evidence=TerminalEvidence.PARTIAL_FILL,
    )

    assert outcome.evidence_finality is EvidenceFinality.NON_TERMINAL
    assert outcome.next_action is OutcomeNextAction.RECONCILE
    assert outcome.is_filled is False
    assert outcome.automatic_resend_allowed is False
    with pytest.raises(ValueError, match="partial fill is non-terminal"):
        _accepted_outcome(evidence=TerminalEvidence.PARTIAL_FILL)


def test_filled_requires_broker_acceptance_but_not_legacy_success() -> None:
    outcome = _accepted_outcome(
        local_recorded=True,
        evidence=TerminalEvidence.FILLED,
    )

    assert outcome.evidence_finality is EvidenceFinality.TERMINAL
    assert outcome.next_action is OutcomeNextAction.NONE
    assert outcome.is_filled is True
    with pytest.raises(
        ValueError, match="execution evidence requires broker acceptance"
    ):
        MutationOutcome(
            request_validated=True,
            mutation_sent=True,
            broker_acceptance=BrokerAcceptance.REJECTED,
            tracking=TrackingState.NOT_APPLICABLE,
            local_recorded=False,
            reconcile_required=False,
            terminal_evidence=TerminalEvidence.FILLED,
        )


def test_terminal_broker_evidence_not_recorded_locally_requires_reconcile() -> None:
    outcome = _accepted_outcome(
        reconcile_required=True,
        evidence=TerminalEvidence.FILLED,
    )

    assert outcome.evidence_finality is EvidenceFinality.TERMINAL
    assert outcome.next_action is OutcomeNextAction.RECONCILE
    with pytest.raises(ValueError, match="not recorded locally"):
        _accepted_outcome(evidence=TerminalEvidence.FILLED)


def test_dispatch_and_acceptance_axes_cannot_contradict_each_other() -> None:
    with pytest.raises(ValueError, match="unsent mutation must have"):
        MutationOutcome(
            request_validated=True,
            mutation_sent=False,
            broker_acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=False,
            reconcile_required=False,
            terminal_evidence=TerminalEvidence.NONE,
        )
    with pytest.raises(ValueError, match="unvalidated request cannot be sent"):
        MutationOutcome(
            request_validated=False,
            mutation_sent=True,
            broker_acceptance=BrokerAcceptance.UNKNOWN,
            tracking=TrackingState.UNKNOWN,
            local_recorded=False,
            reconcile_required=True,
            terminal_evidence=TerminalEvidence.NONE,
        )


def test_next_action_algebra_contains_no_resend_variant() -> None:
    assert {action.value for action in OutcomeNextAction} == {
        "none",
        "track",
        "reconcile",
        "review_new_command",
    }
