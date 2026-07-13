from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
from tests.services.paper_cohort.test_runner_recovery import RecoveringAdapter
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
                enablement=lambda _mode: True,
            ).run(invocation)
            await session.commit()
            return result

    first, second = await asyncio.gather(worker(), worker())

    assert first == second
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
            enablement=lambda _mode: True,
        ).run(CohortRunInvocation(**base, mode=RunMode.PAPER_ACTIVE))
    assert exc_info.value.reason_code == "invocation_conflict"
    assert len(capture.calls) == 1
    assert verifier.calls == []


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
                enablement=lambda _mode: True,
            ).run(invocation)
            await session.commit()
            return result

    first, second = await asyncio.gather(worker(), worker())

    assert first == second
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
