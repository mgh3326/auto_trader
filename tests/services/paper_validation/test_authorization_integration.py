from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.paper_validation import StrategyHypothesisDraft
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOrderRequest,
    VerifiedExperimentProvenance,
    VerifiedPaperOrderIntent,
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

pytestmark = pytest.mark.integration


class _ForbiddenVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, request: PaperOrderRequest) -> VerifiedExperimentProvenance:
        self.calls += 1
        raise AssertionError("forbidden validation must not reach provenance")


class _ForbiddenAdapter:
    broker = Broker.BINANCE

    def __init__(self) -> None:
        self.adapter_calls = 0
        self.client_calls = 0
        self.ledger_calls = 0

    async def submit(self, intent: VerifiedPaperOrderIntent):  # noqa: ANN201
        self.adapter_calls += 1
        self.client_calls += 1
        self.ledger_calls += 1
        raise AssertionError("forbidden validation must not reach an adapter")


class _ForbiddenRegistry:
    def __init__(self, adapter: _ForbiddenAdapter) -> None:
        self.adapter = adapter
        self.broker_calls = 0

    def resolve(self, broker: Broker) -> _ForbiddenAdapter:
        self.broker_calls += 1
        return self.adapter


def _paper_order_request(identity: ValidationIdentity) -> PaperOrderRequest:
    return PaperOrderRequest(
        intent_id="intent-forbidden",
        experiment_id=identity.experiment_id,
        run_id="run-forbidden",
        cohort_id=identity.cohort_id,
        strategy_version_id=identity.strategy_version_id,
        strategy_hash=identity.strategy_hash,
        config_hash=identity.config_hash,
        policy_hash=identity.policy_hash,
        venue=Broker.BINANCE,
        account_mode="demo",
        product="spot",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        notional=Decimal("10"),
        market_snapshot_id="snapshot-forbidden",
        market_snapshot_hash="sha256:snapshot-forbidden",
        market_snapshot_as_of=datetime(2026, 7, 13, tzinfo=UTC),
        market_snapshot_source="binance_public_spot",
    )


async def _rob849_contract_harness(
    validation: PaperValidationService,
    execution: PaperExecutionApplication,
    caller_id: str,
    identity: ValidationIdentity,
) -> None:
    """Exercise the future ROB-849 ordering without implementing its verifier."""
    await validation.authorize_order_submission(caller_id, identity)
    await execution.submit(_paper_order_request(identity))


class _LockProbeFrozenProvider(FakeFrozenInputHashProvider):
    async def get_stamp(self, identity: ValidationIdentity):  # noqa: ANN201
        async with AsyncSessionLocal() as probe, probe.begin():
            acquired = await probe.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended(:validation_id, 0))"
                ),
                {"validation_id": identity.validation_id},
            )
        if not acquired:
            raise RuntimeError("provider invoked while validation lock was held")
        return await super().get_stamp(identity)


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
        ("system-1", "hypothesis"),
        ("researcher-1", "review"),
        ("operator-1", "review"),
        ("system-1", "review"),
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
        "system-1": ActorRole.SYSTEM,
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
async def test_system_role_can_register_and_transition(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app = _service(db_session, validation_identity, {"system-1": ActorRole.SYSTEM})

    event = await app.transition(
        "system-1",
        _transition(validation_identity, ValidationState.DRAFT, None),
    )

    assert event.actor_id == "system-1"
    assert event.actor_role == "system"


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
        select(func.count())
        .select_from(StrategyHypothesisDraft)
        .where(
            StrategyHypothesisDraft.validation_id == validation_identity.validation_id
        )
    )
    assert count == 1


@pytest.mark.parametrize("role", [ActorRole.RESEARCHER, ActorRole.REVIEWER])
@pytest.mark.asyncio
async def test_forbidden_order_authorization_has_zero_external_side_effects(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    role: ActorRole,
) -> None:
    app = _service(db_session, validation_identity, {"caller-1": role})
    verifier = _ForbiddenVerifier()
    adapter = _ForbiddenAdapter()
    registry = _ForbiddenRegistry(adapter)
    execution = PaperExecutionApplication(registry=registry, verifier=verifier)

    with pytest.raises(PaperValidationError, match="forbidden"):
        await _rob849_contract_harness(app, execution, "caller-1", validation_identity)

    assert verifier.calls == 0
    assert registry.broker_calls == 0
    assert adapter.adapter_calls == 0
    assert adapter.client_calls == 0
    assert adapter.ledger_calls == 0


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
async def test_external_evidence_providers_run_before_validation_lock(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    seed = _service(
        db_session,
        validation_identity,
        {
            "researcher-1": ActorRole.RESEARCHER,
            "operator-1": ActorRole.OPERATOR,
        },
    )
    await _advance_to(seed, validation_identity, ValidationState.PAPER_ACTIVE)
    await db_session.commit()

    app = PaperValidationService(
        db_session,
        actor_role_provider=FakeActorRoleProvider(
            {
                "researcher-1": ActorRole.RESEARCHER,
                "operator-1": ActorRole.OPERATOR,
            }
        ),
        frozen_input_provider=_LockProbeFrozenProvider(validation_identity.input_hash),
        policy_provider=FakePolicyHashProvider(validation_identity.policy_hash),
    )

    hypothesis = await app.append_hypothesis(
        "researcher-1", _hypothesis(validation_identity.validation_id)
    )
    await db_session.commit()
    authorization = await app.authorize_order_submission(
        "operator-1", validation_identity
    )

    assert hypothesis.validation_id == validation_identity.validation_id
    assert authorization.state is ValidationState.PAPER_ACTIVE


@pytest.mark.asyncio
async def test_promotion_requires_explicit_exact_confirmation(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app = _service(db_session, validation_identity, {"operator-1": ActorRole.OPERATOR})
    await _advance_to(app, validation_identity, ValidationState.PROMOTION_ELIGIBLE)

    assert "_promotion_confirmed" not in inspect.signature(app.transition).parameters

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
    replay = await app.confirm_promotion(
        "operator-1",
        PromotionConfirmationInput(
            identity=validation_identity,
            idempotency_key="promotion-confirm-ok",
            reason="operator reviewed exact frozen evidence",
            evidence_ids=("operator-confirmation",),
        ),
    )
    assert replay.id == event.id
