from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.paper_validation.contracts import (
    ActorIdentity,
    ActorRole,
    FrozenInputStamp,
    HypothesisDraftInput,
    PaperOrderAuthorization,
    PolicyStamp,
    PostmortemReviewInput,
    TransitionRequest,
    ValidationIdentity,
    ValidationState,
)

HASH = "a" * 64


def identity_payload() -> dict[str, object]:
    return {
        "validation_id": "validation-1",
        "validation_version": 1,
        "experiment_id": HASH,
        "strategy_version_id": "strategy-v1",
        "cohort_id": "cohort-opaque-1",
        "experiment_hash": HASH,
        "cohort_hash": "b" * 64,
        "strategy_hash": "c" * 64,
        "config_hash": "d" * 64,
        "policy_hash": "e" * 64,
        "input_hash": "f" * 64,
    }


def test_identity_is_frozen_and_exactly_binds_experiment_hash() -> None:
    identity = ValidationIdentity(**identity_payload())

    assert identity.experiment_hash == identity.experiment_id
    with pytest.raises(ValidationError):
        identity.validation_id = "changed"  # type: ignore[misc]

    with pytest.raises(ValidationError, match="experiment_hash"):
        ValidationIdentity(**(identity_payload() | {"experiment_hash": "0" * 64}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("experiment_hash", "A" * 64),
        ("cohort_hash", "short"),
        ("strategy_hash", "z" * 64),
        ("config_hash", ""),
        ("policy_hash", None),
        ("input_hash", "1" * 63),
    ],
)
def test_identity_rejects_non_canonical_sha256(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ValidationIdentity(**(identity_payload() | {field: value}))


def test_transition_payload_cannot_spoof_actor_identity_or_role() -> None:
    payload = {
        "identity": identity_payload(),
        "expected_prior_state": "draft",
        "target_state": "offline_eligible",
        "idempotency_key": "transition-1",
        "reason_code": "offline_gate_passed",
        "reason_text": "deterministic offline gate evidence accepted",
        "evidence_ids": ["trial-7", "gate-artifact-2"],
    }

    request = TransitionRequest(**payload)
    assert request.evidence_ids == ("trial-7", "gate-artifact-2")
    for spoof in (
        {"actor_id": "operator"},
        {"actor_role": "system"},
        {"role": "operator"},
    ):
        with pytest.raises(ValidationError, match="Extra inputs"):
            TransitionRequest(**(payload | spoof))


def test_verified_provider_stamps_require_positive_verification() -> None:
    assert FrozenInputStamp(
        bundle_id="bundle-1", content_hash=HASH, verified=True
    ).verified
    assert PolicyStamp(version="policy-v1", content_hash=HASH, verified=True).verified

    with pytest.raises(ValidationError):
        FrozenInputStamp(bundle_id="bundle-1", content_hash=HASH, verified=False)
    with pytest.raises(ValidationError):
        PolicyStamp(version="policy-v1", content_hash=HASH, verified=False)


def test_actor_identity_is_server_owned_and_closed_to_four_roles() -> None:
    assert ActorIdentity(actor_id="agent-1", role=ActorRole.RESEARCHER).role == (
        ActorRole.RESEARCHER
    )
    with pytest.raises(ValidationError):
        ActorIdentity(actor_id="agent-1", role="admin")  # type: ignore[arg-type]


def test_hypothesis_schema_is_fixed_and_complete() -> None:
    hypothesis = HypothesisDraftInput(
        validation_id="validation-1",
        idempotency_key="hypothesis-1",
        mechanism="mean reversion after liquidity shock",
        universe=("BTCUSDT", "ETHUSDT"),
        horizon="4h",
        entry_criteria=("zscore <= -2",),
        exit_criteria=("zscore >= 0",),
        invalidation_criteria=("spread > 50bps",),
        data_requirements=("closed 1h candles",),
        expected_cost_hurdle=Decimal("0.0030"),
        turnover_bound=Decimal("2.0"),
        risk_bound=Decimal("0.02"),
        cited_evidence=("trial-7",),
    )

    assert hypothesis.expected_cost_hurdle == Decimal("0.0030")
    with pytest.raises(ValidationError):
        HypothesisDraftInput(
            **(
                hypothesis.model_dump()
                | {"metrics": {"sharpe": 9}, "actor_role": "operator"}
            )
        )


def test_review_payload_cannot_supply_metrics_gate_or_strategy_payload() -> None:
    base = {
        "validation_id": "validation-1",
        "idempotency_key": "review-1",
        "review_text": "Execution costs invalidate the original mechanism.",
        "cited_evidence": ("paper-run-4",),
    }
    review = PostmortemReviewInput(**base)
    assert review.cited_evidence == ("paper-run-4",)

    for forbidden in ("metrics", "gate_results", "active_strategy_payload"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            PostmortemReviewInput(**(base | {forbidden: {"tampered": True}}))


def test_order_authorization_is_frozen_and_exact() -> None:
    auth = PaperOrderAuthorization(
        identity=ValidationIdentity(**identity_payload()),
        state=ValidationState.PAPER_ACTIVE,
        actor=ActorIdentity(actor_id="operator-1", role=ActorRole.OPERATOR),
        authorization_id="authorization-1",
    )

    assert auth.state is ValidationState.PAPER_ACTIVE
    with pytest.raises(ValidationError):
        auth.authorization_id = "changed"  # type: ignore[misc]
