"""ROB-1036 §4.3 — post-fill completion gate and no-resend timeout recovery.

Offline: a fake transport counts what a real one would have been asked to do.
Nothing here touches a broker, an account, the network, or the runtime DB.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.services.invalid_sample_eligibility.post_fill import (
    FillEvidenceCompleteness,
    PositionEffectEvidence,
    PostFillCompletion,
    PostFillCompletionStatus,
    PostFillManualReviewReason,
    RecoveryAction,
    classify_position_effect,
    evaluate_post_fill_completion,
    plan_timeout_recovery,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeBrokerTransport:
    """Counts the mutations a recovery path would have performed."""

    post_count: int = 0
    issued_identities: list[str] = field(default_factory=list)
    evidence_lookups: list[str] = field(default_factory=list)

    def post_order(self, client_order_id: str) -> None:  # pragma: no cover - guard
        self.post_count += 1
        self.issued_identities.append(client_order_id)

    def lookup_order_by_client_order_id(self, client_order_id: str) -> dict[str, str]:
        self.evidence_lookups.append(client_order_id)
        return {"client_order_id": client_order_id, "status": "filled"}


def execute_recovery(plan, transport: FakeBrokerTransport) -> dict[str, str] | None:
    """Minimal driver: the plan decides, the transport only obeys."""

    assert plan.broker_post_allowed is False
    assert plan.new_identity_allowed is False
    if plan.action is RecoveryAction.LOOKUP_EXISTING_ORDER_EVIDENCE:
        return transport.lookup_order_by_client_order_id(plan.client_order_id)
    return None


# --- §4.3-7: both evidences required --------------------------------------


def test_complete_only_when_both_evidences_hold() -> None:
    result = evaluate_post_fill_completion(
        fill_evidence=FillEvidenceCompleteness.COMPLETE,
        position_effect=PositionEffectEvidence.CONSISTENT,
    )
    assert result.status is PostFillCompletionStatus.COMPLETE
    assert result.reason is None
    assert result.is_terminal_success is True


@pytest.mark.parametrize(
    ("fill_evidence", "position_effect", "expected_reason"),
    [
        (
            FillEvidenceCompleteness.ABSENT,
            PositionEffectEvidence.CONSISTENT,
            PostFillManualReviewReason.ABSENT_FILL_EVIDENCE,
        ),
        (
            FillEvidenceCompleteness.INCOMPLETE,
            PositionEffectEvidence.CONSISTENT,
            PostFillManualReviewReason.INCOMPLETE_FILL_EVIDENCE,
        ),
        (
            FillEvidenceCompleteness.COMPLETE,
            PositionEffectEvidence.ABSENT,
            PostFillManualReviewReason.ABSENT_POSITION_EFFECT_EVIDENCE,
        ),
        (
            FillEvidenceCompleteness.COMPLETE,
            PositionEffectEvidence.INCONSISTENT,
            PostFillManualReviewReason.INCONSISTENT_POSITION_EFFECT_EVIDENCE,
        ),
    ],
)
def test_every_missing_evidence_is_typed_manual_review(
    fill_evidence: FillEvidenceCompleteness,
    position_effect: PositionEffectEvidence,
    expected_reason: PostFillManualReviewReason,
) -> None:
    result = evaluate_post_fill_completion(
        fill_evidence=fill_evidence, position_effect=position_effect
    )
    assert result.status is PostFillCompletionStatus.MANUAL_REVIEW
    assert result.reason is expected_reason
    assert result.is_terminal_success is False


def test_exactly_one_of_nine_evidence_combinations_is_terminal_success() -> None:
    """`filled` status alone never completes: 8 of 9 combinations refuse."""

    successes = [
        (fill, position)
        for fill, position in itertools.product(
            FillEvidenceCompleteness, PositionEffectEvidence
        )
        if evaluate_post_fill_completion(
            fill_evidence=fill, position_effect=position
        ).is_terminal_success
    ]
    assert successes == [
        (FillEvidenceCompleteness.COMPLETE, PositionEffectEvidence.CONSISTENT)
    ]


def test_manual_review_outcome_requires_a_reason() -> None:
    with pytest.raises(ValueError):
        PostFillCompletion(PostFillCompletionStatus.MANUAL_REVIEW, None)


def test_complete_outcome_rejects_a_reason() -> None:
    with pytest.raises(ValueError):
        PostFillCompletion(
            PostFillCompletionStatus.COMPLETE,
            PostFillManualReviewReason.ABSENT_FILL_EVIDENCE,
        )


def test_manual_review_reason_enum_has_no_success_member() -> None:
    """Every reason is a refusal — there is no ``none``/``ok`` escape hatch."""

    values = {member.value for member in PostFillManualReviewReason}
    assert not (values & {"none", "ok", "complete", "success", "n_a"})
    for member in PostFillManualReviewReason:
        # A reason is only constructible alongside a manual-review status.
        assert (
            PostFillCompletion(
                PostFillCompletionStatus.MANUAL_REVIEW, member
            ).is_terminal_success
            is False
        )
        with pytest.raises(ValueError):
            PostFillCompletion(PostFillCompletionStatus.COMPLETE, member)


# --- partial fill after a crash -------------------------------------------


def test_partial_fill_after_crash_is_not_completed() -> None:
    """Cumulative 1, only 0.4 observed → incomplete fill set → manual review."""

    result = evaluate_post_fill_completion(
        fill_evidence=FillEvidenceCompleteness.INCOMPLETE,
        position_effect=PositionEffectEvidence.CONSISTENT,
    )
    assert result.reason is PostFillManualReviewReason.INCOMPLETE_FILL_EVIDENCE


@pytest.mark.parametrize(
    ("side", "before", "after", "filled", "expected"),
    [
        ("sell", "1", "0", "1", PositionEffectEvidence.CONSISTENT),
        ("buy", "0", "1", "1", PositionEffectEvidence.CONSISTENT),
        ("sell", "1", "1", "1", PositionEffectEvidence.INCONSISTENT),
        ("sell", "1", "0", "2", PositionEffectEvidence.INCONSISTENT),
        ("sell", "1", None, "1", PositionEffectEvidence.ABSENT),
        ("sell", None, "0", "1", PositionEffectEvidence.ABSENT),
        ("sell", "1", "0", None, PositionEffectEvidence.ABSENT),
    ],
)
def test_position_effect_classification(
    side: str,
    before: str | None,
    after: str | None,
    filled: str | None,
    expected: PositionEffectEvidence,
) -> None:
    assert (
        classify_position_effect(
            filled_qty=None if filled is None else Decimal(filled),
            qty_before=None if before is None else Decimal(before),
            qty_after=None if after is None else Decimal(after),
            side=side,
        )
        is expected
    )


def test_unobserved_position_is_never_assumed_correct() -> None:
    effect = classify_position_effect(
        filled_qty=Decimal("1"), qty_before=None, qty_after=None, side="sell"
    )
    assert effect is PositionEffectEvidence.ABSENT
    assert (
        evaluate_post_fill_completion(
            fill_evidence=FillEvidenceCompleteness.COMPLETE, position_effect=effect
        ).status
        is PostFillCompletionStatus.MANUAL_REVIEW
    )


# --- §4.3-8: timeout recovery makes no POST and no new identity -----------


def test_timeout_recovery_reads_the_same_identity_and_posts_nothing() -> None:
    transport = FakeBrokerTransport()
    plan = plan_timeout_recovery(
        client_order_id="cleanup-uber-001", evidence_lookup_available=True
    )

    assert plan.action is RecoveryAction.LOOKUP_EXISTING_ORDER_EVIDENCE
    evidence = execute_recovery(plan, transport)

    assert evidence == {"client_order_id": "cleanup-uber-001", "status": "filled"}
    assert transport.post_count == 0
    assert transport.issued_identities == []
    assert transport.evidence_lookups == ["cleanup-uber-001"]


def test_repeated_recovery_never_increments_the_post_count() -> None:
    transport = FakeBrokerTransport()
    for _ in range(5):
        execute_recovery(
            plan_timeout_recovery(
                client_order_id="cleanup-uber-001", evidence_lookup_available=True
            ),
            transport,
        )
    assert transport.post_count == 0
    assert set(transport.evidence_lookups) == {"cleanup-uber-001"}


def test_unavailable_lookup_escalates_and_still_keeps_the_identity() -> None:
    transport = FakeBrokerTransport()
    plan = plan_timeout_recovery(
        client_order_id="cleanup-uber-001", evidence_lookup_available=False
    )
    assert plan.action is RecoveryAction.ESCALATE_MANUAL_REVIEW
    assert plan.client_order_id == "cleanup-uber-001"
    assert execute_recovery(plan, transport) is None
    assert transport.post_count == 0


def test_recovery_action_enum_has_no_resend_variant() -> None:
    values = {member.value for member in RecoveryAction}
    assert not any(
        token in value
        for value in values
        for token in ("resend", "resubmit", "retry", "repost", "new_identity")
    )
