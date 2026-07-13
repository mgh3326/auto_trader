from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_validation import PaperValidationStateTransition
from app.services.paper_validation.contracts import (
    ActorRole,
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


def request(
    identity: ValidationIdentity,
    *,
    target: ValidationState,
    prior: ValidationState | None,
    key: str | None = None,
    reason: str | None = None,
) -> TransitionRequest:
    return TransitionRequest(
        identity=identity,
        expected_prior_state=prior,
        target_state=target,
        idempotency_key=key or f"transition-{uuid4().hex}",
        reason_code=reason or f"advance_to_{target.value}",
        reason_text=f"deterministic evidence permits {target.value}",
        evidence_ids=(f"evidence-{target.value}",),
    )


def service(
    session: AsyncSession,
    identity: ValidationIdentity,
    *,
    role: ActorRole = ActorRole.OPERATOR,
    frozen: FakeFrozenInputHashProvider | None = None,
    policy: FakePolicyHashProvider | None = None,
) -> tuple[
    PaperValidationService,
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
]:
    actors = FakeActorRoleProvider({"caller-1": role})
    frozen = frozen or FakeFrozenInputHashProvider(identity.input_hash)
    policy = policy or FakePolicyHashProvider(identity.policy_hash)
    return (
        PaperValidationService(
            session,
            actor_role_provider=actors,
            frozen_input_provider=frozen,
            policy_provider=policy,
        ),
        actors,
        frozen,
        policy,
    )


async def count_transitions(session: AsyncSession, validation_id: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(PaperValidationStateTransition)
        .where(PaperValidationStateTransition.validation_id == validation_id)
    )
    return int(count or 0)


@pytest.mark.asyncio
async def test_register_then_complete_ordered_history(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app, _, frozen, policy = service(db_session, validation_identity)
    current: ValidationState | None = None
    path = [
        ValidationState.DRAFT,
        ValidationState.OFFLINE_ELIGIBLE,
        ValidationState.SHADOW_SOAK,
        ValidationState.PAPER_ACTIVE,
        ValidationState.PROMOTION_ELIGIBLE,
        ValidationState.PROMOTED,
    ]

    for target in path:
        if target is ValidationState.PROMOTED:
            event = await app.confirm_promotion(
                "caller-1",
                PromotionConfirmationInput(
                    identity=validation_identity,
                    idempotency_key=f"confirm-{uuid4().hex}",
                    reason="operator explicitly confirmed frozen evidence",
                    evidence_ids=("operator-confirmation",),
                ),
            )
        else:
            event = await app.transition(
                "caller-1",
                request(validation_identity, target=target, prior=current),
            )
        current = target
        assert event.new_state == target.value

    history = await app.get_history("caller-1", validation_identity.validation_id)
    assert [event.sequence for event in history] == [1, 2, 3, 4, 5, 6]
    assert [event.new_state for event in history] == [state.value for state in path]
    assert all(event.actor_id == "caller-1" for event in history)
    assert all(event.actor_role == "operator" for event in history)
    assert all(event.input_bundle_id == "bundle-1" for event in history)
    assert all(event.policy_version == "policy-v1" for event in history)
    assert len(frozen.calls) == len(path)
    assert len(policy.calls) == len(path)


@pytest.mark.parametrize("role", [ActorRole.RESEARCHER, ActorRole.REVIEWER])
@pytest.mark.asyncio
async def test_forbidden_transition_has_zero_provider_and_db_calls(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    role: ActorRole,
) -> None:
    app, actors, frozen, policy = service(db_session, validation_identity, role=role)

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=None,
            ),
        )

    assert exc_info.value.reason_code == "forbidden"
    assert actors.calls == ["caller-1"]
    assert frozen.calls == []
    assert policy.calls == []
    assert await count_transitions(db_session, validation_identity.validation_id) == 0


@pytest.mark.asyncio
async def test_unknown_actor_fails_before_evidence_or_db(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    actors = FakeActorRoleProvider({})
    frozen = FakeFrozenInputHashProvider(validation_identity.input_hash)
    policy = FakePolicyHashProvider(validation_identity.policy_hash)
    app = PaperValidationService(
        db_session,
        actor_role_provider=actors,
        frozen_input_provider=frozen,
        policy_provider=policy,
    )

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "unknown",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=None,
            ),
        )

    assert exc_info.value.reason_code == "actor_identity_unavailable"
    assert frozen.calls == []
    assert policy.calls == []


@pytest.mark.parametrize("missing", ["frozen", "policy"])
@pytest.mark.asyncio
async def test_missing_provider_fails_closed(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    missing: str,
) -> None:
    actors = FakeActorRoleProvider({"caller-1": ActorRole.OPERATOR})
    app = PaperValidationService(
        db_session,
        actor_role_provider=actors,
        frozen_input_provider=(
            None
            if missing == "frozen"
            else FakeFrozenInputHashProvider(validation_identity.input_hash)
        ),
        policy_provider=(
            None
            if missing == "policy"
            else FakePolicyHashProvider(validation_identity.policy_hash)
        ),
    )

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=None,
            ),
        )

    assert exc_info.value.reason_code == "evidence_stamp_unavailable"
    assert await count_transitions(db_session, validation_identity.validation_id) == 0


@pytest.mark.parametrize("provider", ["frozen", "policy"])
@pytest.mark.asyncio
async def test_provider_exception_fails_closed_with_stable_reason(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    provider: str,
) -> None:
    frozen = FakeFrozenInputHashProvider(validation_identity.input_hash)
    policy = FakePolicyHashProvider(validation_identity.policy_hash)
    if provider == "frozen":
        frozen.error = RuntimeError("snapshot backend unavailable")
    else:
        policy.error = RuntimeError("policy backend unavailable")
    app, _, _, _ = service(
        db_session,
        validation_identity,
        frozen=frozen,
        policy=policy,
    )

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=None,
            ),
        )

    assert exc_info.value.reason_code == "evidence_stamp_unavailable"


@pytest.mark.parametrize("provider", ["frozen", "policy"])
@pytest.mark.asyncio
async def test_verified_hash_mismatch_fails_closed(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
    provider: str,
) -> None:
    frozen = FakeFrozenInputHashProvider(validation_identity.input_hash)
    policy = FakePolicyHashProvider(validation_identity.policy_hash)
    if provider == "frozen":
        frozen.content_hash = stable_hash("mismatch-input")
    else:
        policy.content_hash = stable_hash("mismatch-policy")
    app, _, _, _ = service(
        db_session,
        validation_identity,
        frozen=frozen,
        policy=policy,
    )

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=None,
            ),
        )

    assert exc_info.value.reason_code == "evidence_hash_mismatch"
    assert await count_transitions(db_session, validation_identity.validation_id) == 0


@pytest.mark.asyncio
async def test_sequential_duplicate_returns_original_event(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app, _, _, _ = service(db_session, validation_identity)
    transition = request(
        validation_identity,
        target=ValidationState.DRAFT,
        prior=None,
        key="same-key",
    )

    first = await app.transition("caller-1", transition)
    second = await app.transition("caller-1", transition)

    assert second.id == first.id
    assert await count_transitions(db_session, validation_identity.validation_id) == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_payload_conflicts(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app, _, _, _ = service(db_session, validation_identity)
    first = request(
        validation_identity,
        target=ValidationState.DRAFT,
        prior=None,
        key="same-key",
    )
    await app.transition("caller-1", first)

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=None,
                key="same-key",
                reason="changed-reason",
            ),
        )

    assert exc_info.value.reason_code == "idempotency_conflict"
    assert await count_transitions(db_session, validation_identity.validation_id) == 1


@pytest.mark.asyncio
async def test_skip_reversal_and_terminal_transition_append_nothing(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app, _, _, _ = service(db_session, validation_identity)
    await app.transition(
        "caller-1",
        request(validation_identity, target=ValidationState.DRAFT, prior=None),
    )

    with pytest.raises(PaperValidationError) as skip:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.PAPER_ACTIVE,
                prior=ValidationState.DRAFT,
            ),
        )
    assert skip.value.reason_code == "invalid_transition"
    assert await count_transitions(db_session, validation_identity.validation_id) == 1

    with pytest.raises(PaperValidationError) as reversal:
        await app.transition(
            "caller-1",
            request(
                validation_identity,
                target=ValidationState.DRAFT,
                prior=ValidationState.DRAFT,
            ),
        )
    assert reversal.value.reason_code == "invalid_transition"


@pytest.mark.asyncio
async def test_identity_drift_from_registered_validation_is_rejected(
    db_session: AsyncSession,
    validation_identity: ValidationIdentity,
) -> None:
    app, _, _, _ = service(db_session, validation_identity)
    await app.transition(
        "caller-1",
        request(validation_identity, target=ValidationState.DRAFT, prior=None),
    )
    drifted = validation_identity.model_copy(
        update={"cohort_hash": stable_hash("different-cohort")}
    )

    with pytest.raises(PaperValidationError) as exc_info:
        await app.transition(
            "caller-1",
            request(
                drifted,
                target=ValidationState.OFFLINE_ELIGIBLE,
                prior=ValidationState.DRAFT,
            ),
        )

    assert exc_info.value.reason_code == "experiment_identity_mismatch"
