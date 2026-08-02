"""ROB-1036 — post-fill completion gate for an invalid-sample cleanup leg.

Two independent evidence facts must both hold before a cleanup leg is called
complete:

1. the broker fill evidence is *complete* (the observed fill set accounts for
   the broker's own cumulative quantity — the ROB-953 ``summarize_fill_set``
   invariant), and
2. the position effect is *consistent* with that fill (the post-trade position
   moved by exactly the filled delta).

A ``filled`` status without a complete activity set, or a complete fill set with
no position evidence, is a typed manual-review outcome — never terminal success.

Recovery after a timeout/crash is evidence lookup on the *same*
``client_order_id``.  There is no resend variant, mirroring
``app.services.execution_outcomes.contract.OutcomeNextAction``.

This module is pure: stdlib only, no DB, broker, network, or clock access.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class FillEvidenceCompleteness(StrEnum):
    """Whether the broker fill evidence accounts for the cumulative quantity."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    ABSENT = "absent"


class PositionEffectEvidence(StrEnum):
    """Whether the observed position effect matches the fill."""

    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    ABSENT = "absent"


class PostFillCompletionStatus(StrEnum):
    """Terminal classification of one post-fill completion attempt."""

    COMPLETE = "complete"
    MANUAL_REVIEW = "manual_review"


class PostFillManualReviewReason(StrEnum):
    """Why a completion attempt was refused.

    Every value is a refusal.  There is deliberately no ``none``/``ok`` member,
    so a reason can never be rendered as a success.
    """

    ABSENT_FILL_EVIDENCE = "absent_fill_evidence"
    INCOMPLETE_FILL_EVIDENCE = "incomplete_fill_evidence"
    ABSENT_POSITION_EFFECT_EVIDENCE = "absent_position_effect_evidence"
    INCONSISTENT_POSITION_EFFECT_EVIDENCE = "inconsistent_position_effect_evidence"


class RecoveryAction(StrEnum):
    """Safe next step after a timeout or crash.

    ``RESUBMIT`` intentionally does not exist: a new broker POST or a new order
    identity is a separately approved command, never an automatic consequence of
    losing the response to one already sent.
    """

    LOOKUP_EXISTING_ORDER_EVIDENCE = "lookup_existing_order_evidence"
    ESCALATE_MANUAL_REVIEW = "escalate_manual_review"


@dataclass(frozen=True, slots=True)
class PostFillCompletion:
    """Outcome of the two-evidence completion gate."""

    status: PostFillCompletionStatus
    reason: PostFillManualReviewReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PostFillCompletionStatus):
            raise TypeError("status must be a PostFillCompletionStatus")
        if self.status is PostFillCompletionStatus.COMPLETE:
            if self.reason is not None:
                raise ValueError("a complete outcome carries no manual-review reason")
        elif not isinstance(self.reason, PostFillManualReviewReason):
            raise ValueError("a manual-review outcome requires a typed reason")

    @property
    def is_terminal_success(self) -> bool:
        return self.status is PostFillCompletionStatus.COMPLETE


def evaluate_post_fill_completion(
    *,
    fill_evidence: FillEvidenceCompleteness,
    position_effect: PositionEffectEvidence,
) -> PostFillCompletion:
    """Complete only when *both* evidence facts hold; otherwise manual review."""

    if not isinstance(fill_evidence, FillEvidenceCompleteness):
        raise TypeError("fill_evidence must be a FillEvidenceCompleteness")
    if not isinstance(position_effect, PositionEffectEvidence):
        raise TypeError("position_effect must be a PositionEffectEvidence")

    if fill_evidence is FillEvidenceCompleteness.ABSENT:
        return PostFillCompletion(
            PostFillCompletionStatus.MANUAL_REVIEW,
            PostFillManualReviewReason.ABSENT_FILL_EVIDENCE,
        )
    if fill_evidence is FillEvidenceCompleteness.INCOMPLETE:
        return PostFillCompletion(
            PostFillCompletionStatus.MANUAL_REVIEW,
            PostFillManualReviewReason.INCOMPLETE_FILL_EVIDENCE,
        )
    if position_effect is PositionEffectEvidence.ABSENT:
        return PostFillCompletion(
            PostFillCompletionStatus.MANUAL_REVIEW,
            PostFillManualReviewReason.ABSENT_POSITION_EFFECT_EVIDENCE,
        )
    if position_effect is PositionEffectEvidence.INCONSISTENT:
        return PostFillCompletion(
            PostFillCompletionStatus.MANUAL_REVIEW,
            PostFillManualReviewReason.INCONSISTENT_POSITION_EFFECT_EVIDENCE,
        )
    return PostFillCompletion(PostFillCompletionStatus.COMPLETE, None)


def classify_position_effect(
    *,
    filled_qty: Decimal | None,
    qty_before: Decimal | None,
    qty_after: Decimal | None,
    side: str,
) -> PositionEffectEvidence:
    """Compare the observed position delta against the filled quantity.

    A sell reduces the position by the filled quantity; a buy increases it.  Any
    missing input is ``ABSENT`` — an unobserved position is never assumed to have
    moved correctly.
    """

    normalized_side = side.strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError(f"side must be buy or sell; got {side!r}")
    if filled_qty is None or qty_before is None or qty_after is None:
        return PositionEffectEvidence.ABSENT
    expected_delta = filled_qty if normalized_side == "buy" else -filled_qty
    if (qty_after - qty_before) != expected_delta:
        return PositionEffectEvidence.INCONSISTENT
    return PositionEffectEvidence.CONSISTENT


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """What a timeout/crash recovery is allowed to do.

    ``broker_post_allowed`` and ``new_identity_allowed`` are ``Literal[False]``
    return types, not configurable flags: this plan can never authorise either.
    """

    action: RecoveryAction
    client_order_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, RecoveryAction):
            raise TypeError("action must be a RecoveryAction")
        cleaned = self.client_order_id.strip()
        if not cleaned:
            raise ValueError("client_order_id must be non-empty")
        object.__setattr__(self, "client_order_id", cleaned)

    @property
    def broker_post_allowed(self) -> Literal[False]:
        return False

    @property
    def new_identity_allowed(self) -> Literal[False]:
        return False


def plan_timeout_recovery(
    *, client_order_id: str, evidence_lookup_available: bool
) -> RecoveryPlan:
    """Recover by reading the *existing* identity's evidence, never by resending.

    When evidence lookup is unavailable the plan escalates to manual review; it
    still carries the original ``client_order_id`` so a later attempt cannot
    invent a new one.
    """

    action = (
        RecoveryAction.LOOKUP_EXISTING_ORDER_EVIDENCE
        if evidence_lookup_available
        else RecoveryAction.ESCALATE_MANUAL_REVIEW
    )
    return RecoveryPlan(action=action, client_order_id=client_order_id)


__all__ = [
    "FillEvidenceCompleteness",
    "PositionEffectEvidence",
    "PostFillCompletion",
    "PostFillCompletionStatus",
    "PostFillManualReviewReason",
    "RecoveryAction",
    "RecoveryPlan",
    "classify_position_effect",
    "evaluate_post_fill_completion",
    "plan_timeout_recovery",
]
