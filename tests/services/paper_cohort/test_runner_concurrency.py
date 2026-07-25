from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
)
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_validation.contracts import ActorRole
from app.services.paper_validation.service import PaperValidationService
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_cohort.test_runner_paper_active import (
    FakeNativeResolver,
    FakeRegistry,
    _setup,
)
from tests.services.paper_cohort.test_runner_recovery import (
    AdvancingClock,
    RecoveringAdapter,
)
from tests.services.paper_cohort.test_runner_shadow import (
    FakeCapture,
    FakeQuotes,
    _active_cohort,
)
from tests.services.paper_validation.conftest import (
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


@dataclass
class UnusedVerifier:
    calls: list[object] = field(default_factory=list)

    async def verify(self, request):
        self.calls.append(request)
        raise AssertionError("conflicting replay must not reach verifier")


@pytest.mark.asyncio
async def test_two_postgresql_sessions_create_one_snapshot_and_intent_set(
    db_session,
) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    capture = FakeCapture()
    barrier = asyncio.Barrier(2)
    invocation = CohortRunInvocation(
        cohort_id=cohort_id,
        run_id=f"concurrent-run-{nonce}",
        round_decision_id=f"concurrent-round-{nonce}",
        mode=RunMode.SHADOW,
    )

    async def worker():
        async with AsyncSessionLocal() as session:
            await barrier.wait()
            result = await PaperCohortRunner(
                session,
                capture=capture,
                quote_provider=FakeQuotes(session),
                clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
                enablement=lambda _mode: True,
            ).run(invocation)
            await session.commit()
            return result

    outcomes = await asyncio.gather(worker(), worker(), return_exceptions=True)
    successes = [item for item in outcomes if not isinstance(item, BaseException)]
    conflicts = [item for item in outcomes if isinstance(item, PaperCohortError)]

    assert successes
    assert all(item == successes[0] for item in successes)
    assert all(item.reason_code == "invocation_in_progress" for item in conflicts)
    assert len(capture.calls) == 1
    async with AsyncSessionLocal() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PaperCohortRunClaim)
                .where(PaperCohortRunClaim.run_id == invocation.run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CanonicalMarketSnapshot)
                .where(CanonicalMarketSnapshot.run_id == invocation.run_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PaperCohortDecision)
                .where(PaperCohortDecision.run_id == invocation.run_id)
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PaperCohortVenueIntent)
                .where(PaperCohortVenueIntent.run_id == invocation.run_id)
            )
            == 4
        )


@pytest.mark.asyncio
async def test_same_claim_identity_with_different_request_is_stable_conflict(
    db_session,
) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    capture = FakeCapture()
    base = {
        "cohort_id": cohort_id,
        "run_id": f"conflict-run-{nonce}",
        "round_decision_id": f"conflict-round-{nonce}",
    }
    shadow = PaperCohortRunner(
        db_session,
        capture=capture,
        quote_provider=FakeQuotes(db_session),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    )
    await shadow.run(CohortRunInvocation(**base, mode=RunMode.SHADOW))
    await db_session.commit()
    verifier = UnusedVerifier()

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortRunner(
            db_session,
            capture=capture,
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
            enablement=lambda _mode: True,
        ).run(CohortRunInvocation(**base, mode=RunMode.PAPER_ACTIVE))
    assert exc_info.value.reason_code == "invocation_conflict"
    assert len(capture.calls) == 1
    assert verifier.calls == []


@pytest.mark.asyncio
async def test_live_unexpired_prepared_claim_cannot_be_stolen(db_session) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    invocation = CohortRunInvocation(
        cohort_id=cohort_id,
        run_id=f"live-prepared-run-{nonce}",
        round_decision_id=f"live-prepared-round-{nonce}",
        mode=RunMode.SHADOW,
    )
    first = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    claim, replay = await first._claim(invocation)
    assert claim is not None and replay is None
    cohort, assignments = await first._cohort(cohort_id)
    await first._prepare(invocation, cohort, assignments)
    await db_session.commit()

    contender = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        clock=lambda: CAPTURED_AT + timedelta(minutes=1),
    )
    with pytest.raises(PaperCohortError) as exc_info:
        await contender._claim(invocation)

    assert exc_info.value.reason_code == "invocation_in_progress"


@pytest.mark.asyncio
async def test_checkpoint_rejects_a_stale_owner_token(db_session) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    invocation = CohortRunInvocation(
        cohort_id=cohort_id,
        run_id=f"strict-owner-run-{nonce}",
        round_decision_id=f"strict-owner-round-{nonce}",
        mode=RunMode.SHADOW,
    )
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    claim, replay = await runner._claim(invocation)
    assert claim is not None and replay is None
    claim_id = claim.id
    await db_session.commit()

    with pytest.raises(PaperCohortError) as exc_info:
        await runner._lock_owned_claim(claim_id, "stale-owner-token")

    assert exc_info.value.reason_code == "invocation_owner_mismatch"


@pytest.mark.asyncio
async def test_two_sessions_submit_each_paper_intent_exactly_once(db_session) -> None:
    nonce, activation, _ = await _setup(db_session)
    assignment = activation.assignments[0]
    capture = FakeCapture()
    native = FakeNativeResolver()
    adapters = {
        Broker.BINANCE: RecoveringAdapter(Broker.BINANCE),
        Broker.ALPACA: RecoveringAdapter(Broker.ALPACA),
    }
    barrier = asyncio.Barrier(2)
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"active-concurrent-run-{nonce}",
        round_decision_id=f"active-concurrent-round-{nonce}",
        mode=RunMode.PAPER_ACTIVE,
    )

    async def worker():
        async with AsyncSessionLocal() as session:
            validation = PaperValidationService(
                session,
                actor_role_provider=FakeActorRoleProvider(
                    {"paper-cohort-runner": ActorRole.SYSTEM}
                ),
                frozen_input_provider=FakeFrozenInputHashProvider(
                    assignment.input_hash
                ),
                policy_provider=FakePolicyHashProvider(assignment.policy_hash),
            )
            verifier = PaperCohortProvenanceVerifier(
                session,
                validation_service=validation,
                caller_id="paper-cohort-runner",
                clock=lambda: CAPTURED_AT,
            )

            def app_factory(current_verifier):
                return PaperExecutionApplication(
                    registry=FakeRegistry(adapters), verifier=current_verifier
                )

            await barrier.wait()
            result = await PaperCohortRunner(
                session,
                capture=capture,
                quote_provider=FakeQuotes(session),
                verifier=verifier,
                application_factory=app_factory,
                native_resolver=native,
                clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
                enablement=lambda _mode: True,
            ).run(invocation)
            await session.commit()
            return result

    outcomes = await asyncio.gather(worker(), worker(), return_exceptions=True)
    successes = [item for item in outcomes if not isinstance(item, BaseException)]
    conflicts = [item for item in outcomes if isinstance(item, PaperCohortError)]

    assert successes
    assert all(item == successes[0] for item in successes)
    assert all(item.reason_code == "invocation_in_progress" for item in conflicts)
    assert len(capture.calls) == 1
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 4
    assert sum(adapter.replay_count for adapter in adapters.values()) == 0
    assert len(native.calls) == 4
    async with AsyncSessionLocal() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PaperRunOrderLink)
                .where(PaperRunOrderLink.run_id == invocation.run_id)
            )
            == 4
        )


@dataclass
class BlockingFirstSubmit:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def __call__(self, _result) -> None:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()


@pytest.mark.asyncio
async def test_live_owner_crossing_lease_expiry_is_fenced_without_deadlock(
    db_session,
) -> None:
    nonce, activation, _ = await _setup(db_session)
    assignment = activation.assignments[0]
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"lease-run-{nonce}",
        round_decision_id=f"lease-round-{nonce}",
        mode=RunMode.PAPER_ACTIVE,
    )
    clock = AdvancingClock()
    hook = BlockingFirstSubmit()
    capture = FakeCapture()
    native = FakeNativeResolver()
    adapters = {
        Broker.BINANCE: RecoveringAdapter(Broker.BINANCE),
        Broker.ALPACA: RecoveringAdapter(Broker.ALPACA),
    }

    async def worker(*, block: bool):
        async with AsyncSessionLocal() as session:
            validation = PaperValidationService(
                session,
                actor_role_provider=FakeActorRoleProvider(
                    {"paper-cohort-runner": ActorRole.SYSTEM}
                ),
                frozen_input_provider=FakeFrozenInputHashProvider(
                    assignment.input_hash
                ),
                policy_provider=FakePolicyHashProvider(assignment.policy_hash),
            )
            verifier = PaperCohortProvenanceVerifier(
                session,
                validation_service=validation,
                caller_id="paper-cohort-runner",
                clock=clock,
            )

            def app_factory(current_verifier):
                return PaperExecutionApplication(
                    registry=FakeRegistry(adapters), verifier=current_verifier
                )

            return await PaperCohortRunner(
                session,
                capture=capture,
                quote_provider=FakeQuotes(session),
                verifier=verifier,
                application_factory=app_factory,
                native_resolver=native,
                after_submit_hook=hook if block else None,
                clock=clock,
                enablement=lambda _mode: True,
            ).run(invocation)

    first_task = asyncio.create_task(worker(block=True))
    await asyncio.wait_for(hook.started.wait(), timeout=5)
    clock.now += timedelta(minutes=6)
    second_task = asyncio.create_task(worker(block=False))
    await asyncio.sleep(0.1)
    hook.release.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first_task, second_task, return_exceptions=True), timeout=10
    )

    successes = [item for item in outcomes if not isinstance(item, BaseException)]
    fenced = [item for item in outcomes if isinstance(item, PaperCohortError)]
    assert len(successes) == 1
    assert [item.reason_code for item in fenced] == ["invocation_owner_mismatch"]
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 4
    assert sum(adapter.replay_count for adapter in adapters.values()) == 0
    assert len(capture.calls) == 1
