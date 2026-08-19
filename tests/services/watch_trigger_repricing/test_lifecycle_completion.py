"""ROB-1286 §101차 ⑤ + 완료 기준 — terminals, and "analysis-only is a failure".

The operator's rule: every fire must end as a proposal or as a reason
attributed to that fire. A run that analysed and produced neither has not
finished. These tests make that rule executable rather than aspirational.
"""

from __future__ import annotations

import pytest

from app.services.watch_trigger_repricing.lifecycle import (
    NON_RECLAIMABLE_STATES,
    RESOLVED_LIFECYCLE_STATES,
    TERMINAL_LIFECYCLE_STATES,
    ClaimLifecycle,
    CompletionRow,
    IncompleteOutcome,
    SessionOutcome,
    awaiting_reconcile,
    build_completion_mapping,
    proposal_created,
    rejected,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Exactly two states are *outcomes*, and neither of them is "analysed"
# ---------------------------------------------------------------------------
def test_only_two_states_satisfy_the_completion_criterion() -> None:
    """The load-bearing half: what counts as *done*.

    ROB-1290 r2 added a fourth terminal (``awaiting_reconcile``) so an
    ambiguous spawn survives the TTL instead of being re-judged. That must
    not become a third way to look finished, so the resolved set is
    asserted separately from the terminal set, and it is still the same
    two states the operator named.
    """
    assert {str(s) for s in RESOLVED_LIFECYCLE_STATES} == {
        "proposal_created",
        "rejected_with_reason",
    }


def test_the_extra_terminals_are_faults_and_are_never_resolved() -> None:
    assert {str(s) for s in TERMINAL_LIFECYCLE_STATES} == {
        "proposal_created",
        "rejected_with_reason",
        "expired_unprocessed",
        "awaiting_reconcile",
    }
    faults = TERMINAL_LIFECYCLE_STATES - RESOLVED_LIFECYCLE_STATES
    assert {str(s) for s in faults} == {"expired_unprocessed", "awaiting_reconcile"}
    for fault in faults:
        row = CompletionRow(
            event_uuid="e",
            symbol="005930",
            state=str(fault),
            proposal_id=None,
            rejection_reason=None,
        )
        assert row.is_resolved is False


def test_no_analysis_only_terminal_exists() -> None:
    """§101차 ⑤: not in the enum, and not reachable.

    Guarding the *names* matters because the failure this prevents is a
    future edit adding a comfortable third success state.
    """
    members = {m.value for m in ClaimLifecycle}
    assert members == {
        "started",
        "proposal_created",
        "rejected_with_reason",
        "expired_unprocessed",
        "awaiting_reconcile",
    }
    for forbidden in (
        "analysed",
        "analyzed",
        "reviewed",
        "no_action",
        "report_only",
        "observed",
        "acknowledged",
    ):
        assert forbidden not in members


def test_the_quarantine_terminal_claims_no_knowledge() -> None:
    """It means "nobody knows", so it may not carry evidence either way."""
    assert awaiting_reconcile().state is ClaimLifecycle.AWAITING_RECONCILE
    assert awaiting_reconcile().proposal_id is None
    assert awaiting_reconcile().rejection_reason is None
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.AWAITING_RECONCILE, proposal_id="prop-1")
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(
            state=ClaimLifecycle.AWAITING_RECONCILE, rejection_reason="unknown"
        )


def test_the_ttl_terminal_is_the_only_reclaimable_one() -> None:
    """r2 / BLOCKER 2, stated as a set.

    ``expired_unprocessed`` exists so a crashed tick's fire is picked up
    again. ``awaiting_reconcile`` exists so an ambiguous one is *not*.
    """
    assert {str(s) for s in NON_RECLAIMABLE_STATES} == {
        "proposal_created",
        "rejected_with_reason",
        "awaiting_reconcile",
    }
    assert ClaimLifecycle.EXPIRED_UNPROCESSED not in NON_RECLAIMABLE_STATES


def test_started_is_not_a_terminal() -> None:
    assert ClaimLifecycle.STARTED not in TERMINAL_LIFECYCLE_STATES
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.STARTED)


# ---------------------------------------------------------------------------
# A terminal cannot be reached without its evidence
# ---------------------------------------------------------------------------
def test_proposal_terminal_requires_a_proposal_id() -> None:
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.PROPOSAL_CREATED)
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.PROPOSAL_CREATED, proposal_id="   ")
    assert proposal_created("prop-1").proposal_id == "prop-1"


def test_rejection_terminal_requires_a_non_blank_reason() -> None:
    """A blank reason is the 'analysed and said nothing' shape."""
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.REJECTED_WITH_REASON)
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.REJECTED_WITH_REASON, rejection_reason="")
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(
            state=ClaimLifecycle.REJECTED_WITH_REASON, rejection_reason="\n  \t "
        )
    assert rejected("below policy floor").rejection_reason == "below policy floor"


def test_a_terminal_cannot_carry_both_kinds_of_evidence() -> None:
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(
            state=ClaimLifecycle.PROPOSAL_CREATED,
            proposal_id="p1",
            rejection_reason="also rejected?",
        )


def test_ttl_terminal_carries_neither() -> None:
    assert SessionOutcome(state=ClaimLifecycle.EXPIRED_UNPROCESSED) is not None
    with pytest.raises(IncompleteOutcome):
        SessionOutcome(state=ClaimLifecycle.EXPIRED_UNPROCESSED, proposal_id="p1")


# ---------------------------------------------------------------------------
# The 1:1 mapping table, and what counts as complete
# ---------------------------------------------------------------------------
def test_every_polled_event_appears_in_the_mapping() -> None:
    report = build_completion_mapping(
        polled_event_uuids=[("e1", "005930"), ("e2", "000660")],
        outcomes={"e1": proposal_created("p-1")},
    )

    assert [row.event_uuid for row in report.rows] == ["e1", "e2"]
    assert report.as_table()[0]["proposalId"] == "p-1"


def test_one_unmapped_event_fails_the_whole_run() -> None:
    """The brief: 빈 이벤트가 1건이라도 있으면 실패."""
    report = build_completion_mapping(
        polled_event_uuids=[("e1", "005930"), ("e2", "000660")],
        outcomes={"e1": proposal_created("p-1")},
    )

    assert report.is_complete is False
    assert [row.event_uuid for row in report.unresolved] == ["e2"]
    assert report.as_table()[1]["state"] == "unmapped"


def test_a_rejection_reason_resolves_an_event() -> None:
    report = build_completion_mapping(
        polled_event_uuids=[("e1", "005930"), ("e2", "000660")],
        outcomes={
            "e1": proposal_created("p-1"),
            "e2": rejected("sellable qty 0 after settlement hold"),
        },
    )

    assert report.is_complete is True
    assert report.unresolved == ()


def test_ttl_expiry_does_not_count_as_resolved() -> None:
    """An unjudged fire must not be relabelled as a success."""
    report = build_completion_mapping(
        polled_event_uuids=[("e1", "005930")],
        outcomes={"e1": SessionOutcome(state=ClaimLifecycle.EXPIRED_UNPROCESSED)},
    )

    assert report.is_complete is False
    assert [row.event_uuid for row in report.unresolved] == ["e1"]


def test_an_empty_poll_is_not_silently_complete() -> None:
    """No fires is not the same as 'all fires handled'."""
    report = build_completion_mapping(polled_event_uuids=[], outcomes={})

    assert report.is_complete is False
