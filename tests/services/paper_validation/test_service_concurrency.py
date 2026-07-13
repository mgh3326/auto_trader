from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.models.paper_validation import PaperValidationStateTransition
from app.models.research_backtest import ResearchStrategyExperiment
from app.services.paper_validation.contracts import (
    ActorRole,
    TransitionRequest,
    ValidationIdentity,
    ValidationState,
)
from app.services.paper_validation.service import (
    PaperValidationError,
    PaperValidationService,
)
from tests.services.paper_validation.conftest import (
    HASH_FIELDS,
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
    stable_hash,
)


async def _persisted_identity() -> ValidationIdentity:
    nonce = uuid4().hex
    hashes = {name: stable_hash(f"{nonce}:{name}") for name in HASH_FIELDS}
    experiment = ResearchStrategyExperiment(
        experiment_id=stable_hash(f"{nonce}:experiment"),
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        manifest={},
        **hashes,
    )
    async with AsyncSessionLocal() as session, session.begin():
        session.add(experiment)
    return ValidationIdentity(
        validation_id=f"validation-{uuid4().hex}",
        validation_version=1,
        experiment_id=experiment.experiment_id,
        strategy_version_id=experiment.strategy_version,
        cohort_id="cohort-opaque-1",
        experiment_hash=experiment.experiment_id,
        cohort_hash=stable_hash(f"{nonce}:cohort"),
        strategy_hash=experiment.strategy_hash,
        config_hash=experiment.frozen_config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=stable_hash(f"{nonce}:input"),
    )


def _request(
    identity: ValidationIdentity,
    target: ValidationState,
    prior: ValidationState | None,
    key: str,
) -> TransitionRequest:
    return TransitionRequest(
        identity=identity,
        expected_prior_state=prior,
        target_state=target,
        idempotency_key=key,
        reason_code=f"advance_to_{target.value}",
        reason_text=f"concurrency evidence permits {target.value}",
        evidence_ids=(f"evidence-{target.value}",),
    )


def _service(session, identity: ValidationIdentity) -> PaperValidationService:
    return PaperValidationService(
        session,
        actor_role_provider=FakeActorRoleProvider({"operator-1": ActorRole.OPERATOR}),
        frozen_input_provider=FakeFrozenInputHashProvider(identity.input_hash),
        policy_provider=FakePolicyHashProvider(identity.policy_hash),
    )


async def _run_at_barrier(
    barrier: asyncio.Barrier,
    identity: ValidationIdentity,
    transition: TransitionRequest,
) -> tuple[str, int | str]:
    async with AsyncSessionLocal() as session, session.begin():
        app = _service(session, identity)
        await barrier.wait()
        try:
            event = await app.transition("operator-1", transition)
            event_id = event.id
            return "event", event_id
        except PaperValidationError as exc:
            return "error", exc.reason_code


async def _seed_to(identity: ValidationIdentity, target: ValidationState) -> None:
    path = [
        ValidationState.DRAFT,
        ValidationState.OFFLINE_ELIGIBLE,
        ValidationState.SHADOW_SOAK,
        ValidationState.PAPER_ACTIVE,
        ValidationState.PROMOTION_ELIGIBLE,
    ]
    prior: ValidationState | None = None
    async with AsyncSessionLocal() as session, session.begin():
        app = _service(session, identity)
        for state in path:
            await app.transition(
                "operator-1",
                _request(identity, state, prior, f"seed-{state.value}-{uuid4().hex}"),
            )
            prior = state
            if state is target:
                return
    raise AssertionError(f"target {target} is not in seed path")


@pytest.mark.asyncio
async def test_concurrent_identical_retry_appends_once_and_returns_same_event() -> None:
    identity = await _persisted_identity()
    transition = _request(
        identity, ValidationState.DRAFT, None, "concurrent-duplicate-key"
    )
    barrier = asyncio.Barrier(2)

    results = await asyncio.gather(
        _run_at_barrier(barrier, identity, transition),
        _run_at_barrier(barrier, identity, transition),
    )

    assert results[0][0] == results[1][0] == "event"
    assert results[0][1] == results[1][1]
    async with AsyncSessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(PaperValidationStateTransition)
            .where(
                PaperValidationStateTransition.validation_id == identity.validation_id
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_distinct_terminal_transitions_have_one_winner() -> None:
    identity = await _persisted_identity()
    await _seed_to(identity, ValidationState.PROMOTION_ELIGIBLE)
    barrier = asyncio.Barrier(2)

    results = await asyncio.gather(
        _run_at_barrier(
            barrier,
            identity,
            _request(
                identity,
                ValidationState.ABORTED,
                ValidationState.PROMOTION_ELIGIBLE,
                "abort-key",
            ),
        ),
        _run_at_barrier(
            barrier,
            identity,
            _request(
                identity,
                ValidationState.REJECTED,
                ValidationState.PROMOTION_ELIGIBLE,
                "reject-key",
            ),
        ),
    )

    assert sorted(kind for kind, _ in results) == ["error", "event"]
    assert ("error", "concurrent_transition_conflict") in results
    async with AsyncSessionLocal() as session:
        history = (
            await session.execute(
                select(PaperValidationStateTransition)
                .where(
                    PaperValidationStateTransition.validation_id
                    == identity.validation_id
                )
                .order_by(PaperValidationStateTransition.sequence)
            )
        ).scalars()
        events = list(history)
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5, 6]
    assert events[-1].new_state in {"aborted", "rejected"}
