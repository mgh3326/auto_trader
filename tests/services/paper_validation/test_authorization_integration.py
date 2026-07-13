from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_validation import (
    PaperValidationPostmortemReview,
    StrategyHypothesisDraft,
)
from app.services.paper_validation.contracts import (
    ActorRole,
    HypothesisDraftInput,
    PostmortemReviewInput,
    PromotionConfirmationInput,
    TransitionRequest,
    ValidationIdentity,
    ValidationState,
)
from app.services.paper_validation.service import (
    PaperValidationError,
    PaperValidationService,
)
from tests.services.paper_validation.conftest import (
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
    stable_hash,
)


@dataclass
class ForbiddenSideEffects:
    adapter_calls: list[str] = field(default_factory=list)
    broker_calls: list[str] = field(default_factory=list)
    ledger_calls: list[str] = field(default_factory=list)
    verifier_calls: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(
            len(calls)
            for calls in (
                self.adapter_calls,
                self.broker_calls,
                self.ledger_calls,
                self.verifier_calls,
            )
        )


def _service(
    session: AsyncSession,
    identity: ValidationIdentity,
    roles: dict[str, ActorRole],
) -> PaperValidationService:
    return PaperValidationService(
        session,
        actor_role_provider=FakeActorRoleProvider(roles),
        frozen_input_provider=FakeFrozenInputHashProvider(identity.input_hash),
        policy_provider=FakePolicyHashProvider(identity.policy_hash),
    )


def _transition(
    identity: ValidationIdentity,
    target: ValidationState,
    prior: ValidationState | None,
) -> TransitionRequest:
    return TransitionRequest(
        identity=identity,
        expected_prior_state=prior,
        target_state=target,
        idempotency_key=f"{target.value}-{uuid4().hex}",
        reason_code=f"advance_to_{target.value}",
        reason_text="deterministic quantitative gate passed",
        evidence_ids=(f"gate-{target.value}",),
    )


def _hypothesis(
    validation_id: str, *, key: str = "hypothesis-1"
) -> HypothesisDraftInput:
    return HypothesisDraftInput(
        validation_id=validation_id,
        idempotency_key=key,
        mechanism="liquidity recovery after a bounded dislocation",
        universe=("KRX:005930",),
        horizon="5 trading days",
        entry_criteria=("close below lower band",),
        exit_criteria=("mean reversion target",),
        invalidation_criteria=("volatility regime break",),
        data_requirements=("point-in-time daily bars",),
        expected_cost_hurdle=Decimal("0.003"),
        turnover_bound=Decimal("0.25"),
        risk_bound=Decimal("0.02"),
        cited_evidence=("evidence-research-1",),
    )


async def _advance_to(
    app: PaperValidationService,
    identity: ValidationIdentity,
    target: ValidationState,
) -> None:
    path = (
        ValidationState.DRAFT,
        ValidationState.OFFLINE_ELIGIBLE,
        ValidationState.SHADOW_SOAK,
        ValidationState.PAPER_ACTIVE,
        ValidationState.PROMOTION_ELIGIBLE,
    )
    prior: ValidationState | None = None
    for state in path:
        await app.transition("operator-1", _transition(identity, state, prior))
        prior = state
        if state is target:
            return


@pytest.mark.asyncio
async def test_role_separated_hypothesis_review_and_complete_audit(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app = _service(
        db_session,
        validation_identity,
        {
            "researcher-1": ActorRole.RESEARCHER,
            "reviewer-1": ActorRole.REVIEWER,
            "operator-1": ActorRole.OPERATOR,
        },
    )
    await _advance_to(app, validation_identity, ValidationState.DRAFT)

    hypothesis = await app.append_hypothesis(
        "researcher-1", _hypothesis(validation_identity.validation_id)
    )
    review = await app.append_postmortem_review(
        "reviewer-1",
        PostmortemReviewInput(
            validation_id=validation_identity.validation_id,
            idempotency_key="review-1",
            review_text="The invalidation evidence remains falsifiable.",
            cited_evidence=("review-evidence-1",),
        ),
    )
    audit = await app.get_audit("researcher-1", validation_identity.validation_id)

    assert hypothesis.author_id == "researcher-1"
    assert review.evaluator_id == "reviewer-1"
    assert audit["transitions"][0].actor_id == "operator-1"
    assert audit["hypotheses"] == [hypothesis]
    assert audit["reviews"] == [review]


@pytest.mark.parametrize(
    ("caller", "operation"),
    [
        ("reviewer-1", "hypothesis"),
        ("operator-1", "hypothesis"),
        ("researcher-1", "review"),
        ("operator-1", "review"),
    ],
)
@pytest.mark.asyncio
async def test_narrative_append_role_matrix_fails_closed(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    caller: str,
    operation: str,
) -> None:
    roles = {
        "researcher-1": ActorRole.RESEARCHER,
        "reviewer-1": ActorRole.REVIEWER,
        "operator-1": ActorRole.OPERATOR,
    }
    app = _service(db_session, validation_identity, roles)
    await _advance_to(app, validation_identity, ValidationState.DRAFT)

    with pytest.raises(PaperValidationError, match="forbidden"):
        if operation == "hypothesis":
            await app.append_hypothesis(
                caller, _hypothesis(validation_identity.validation_id)
            )
        else:
            await app.append_postmortem_review(
                caller,
                PostmortemReviewInput(
                    validation_id=validation_identity.validation_id,
                    idempotency_key="review-forbidden",
                    review_text="forbidden narrative",
                    cited_evidence=("evidence",),
                ),
            )


@pytest.mark.asyncio
async def test_narrative_idempotency_replays_and_conflicts(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app = _service(
        db_session,
        validation_identity,
        {"researcher-1": ActorRole.RESEARCHER, "operator-1": ActorRole.OPERATOR},
    )
    await _advance_to(app, validation_identity, ValidationState.DRAFT)
    first = await app.append_hypothesis(
        "researcher-1", _hypothesis(validation_identity.validation_id)
    )
    replay = await app.append_hypothesis(
        "researcher-1", _hypothesis(validation_identity.validation_id)
    )

    assert replay.id == first.id
    with pytest.raises(PaperValidationError, match="idempotency_conflict"):
        await app.append_hypothesis(
            "researcher-1",
            _hypothesis(validation_identity.validation_id).model_copy(
                update={"mechanism": "changed mechanism"}
            ),
        )
    count = await db_session.scalar(
        select(func.count()).select_from(StrategyHypothesisDraft)
    )
    assert count == 1


@pytest.mark.parametrize("role", [ActorRole.RESEARCHER, ActorRole.REVIEWER])
@pytest.mark.asyncio
async def test_forbidden_order_authorization_has_zero_external_side_effects(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    role: ActorRole,
) -> None:
    effects = ForbiddenSideEffects()
    app = _service(db_session, validation_identity, {"caller-1": role})

    with pytest.raises(PaperValidationError, match="forbidden"):
        await app.authorize_order_submission("caller-1", validation_identity)

    assert effects.total == 0


@pytest.mark.asyncio
async def test_order_authorization_requires_allowed_state_and_exact_identity(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app = _service(db_session, validation_identity, {"operator-1": ActorRole.OPERATOR})
    await _advance_to(app, validation_identity, ValidationState.DRAFT)

    with pytest.raises(PaperValidationError, match="order_state_not_authorized"):
        await app.authorize_order_submission("operator-1", validation_identity)

    await app.transition(
        "operator-1",
        _transition(
            validation_identity,
            ValidationState.OFFLINE_ELIGIBLE,
            ValidationState.DRAFT,
        ),
    )
    await app.transition(
        "operator-1",
        _transition(
            validation_identity,
            ValidationState.SHADOW_SOAK,
            ValidationState.OFFLINE_ELIGIBLE,
        ),
    )
    await app.transition(
        "operator-1",
        _transition(
            validation_identity,
            ValidationState.PAPER_ACTIVE,
            ValidationState.SHADOW_SOAK,
        ),
    )
    authorization = await app.authorize_order_submission(
        "operator-1", validation_identity
    )
    assert authorization.state is ValidationState.PAPER_ACTIVE
    assert authorization.identity == validation_identity

    mismatch = validation_identity.model_copy(
        update={"input_hash": stable_hash("different-current-input")}
    )
    with pytest.raises(PaperValidationError, match="authorization_identity_mismatch"):
        await app.authorize_order_submission("operator-1", mismatch)


@pytest.mark.asyncio
async def test_promotion_requires_explicit_exact_confirmation(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app = _service(db_session, validation_identity, {"operator-1": ActorRole.OPERATOR})
    await _advance_to(app, validation_identity, ValidationState.PROMOTION_ELIGIBLE)

    with pytest.raises(PaperValidationError, match="promotion_confirmation_required"):
        await app.transition(
            "operator-1",
            _transition(
                validation_identity,
                ValidationState.PROMOTED,
                ValidationState.PROMOTION_ELIGIBLE,
            ),
        )

    mismatch = validation_identity.model_copy(
        update={"cohort_hash": stable_hash("changed-cohort")}
    )
    with pytest.raises(PaperValidationError, match="promotion_confirmation_mismatch"):
        await app.confirm_promotion(
            "operator-1",
            PromotionConfirmationInput(
                identity=mismatch,
                idempotency_key="promotion-confirm-mismatch",
                reason="operator reviewed exact frozen evidence",
                evidence_ids=("operator-confirmation",),
            ),
        )

    event = await app.confirm_promotion(
        "operator-1",
        PromotionConfirmationInput(
            identity=validation_identity,
            idempotency_key="promotion-confirm-ok",
            reason="operator reviewed exact frozen evidence",
            evidence_ids=("operator-confirmation",),
        ),
    )
    assert event.new_state == "promoted"


def test_review_model_has_no_llm_controlled_metric_or_gate_fields() -> None:
    assert "metrics" not in PaperValidationPostmortemReview.__table__.columns
    assert "gate_results" not in PaperValidationPostmortemReview.__table__.columns
    assert (
        "active_strategy_payload"
        not in PaperValidationPostmortemReview.__table__.columns
    )
