from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select

from app.models.paper_cohort import PaperRunOrderLink
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
)
from app.services.paper_cohort.contracts import RunMode
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_cohort.test_runner_paper_active import (
    FakeNativeResolver,
    FakeRegistry,
    _setup,
)
from tests.services.paper_cohort.test_runner_shadow import FakeCapture, FakeQuotes
from tests.services.paper_validation.conftest import stable_hash

pytestmark = pytest.mark.integration


@dataclass
class RecoveringAdapter:
    broker: Broker
    broker_posts: int = 0
    replay_count: int = 0
    results: dict[str, PaperOperationResult] = field(default_factory=dict)

    async def submit(self, intent):
        if intent.idempotency_key in self.results:
            self.replay_count += 1
            return self.results[intent.idempotency_key].model_copy(
                update={"replayed": True}
            )
        self.broker_posts += 1
        suffix = stable_hash(f"{self.broker.value}:{intent.idempotency_key}")[:16]
        result = PaperOperationResult(
            operation=PaperOperation.SUBMIT,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code="ok",
            venue=self.broker,
            native_order_id=f"broker-{suffix}",
            native_client_order_id=f"client-{suffix}",
        )
        self.results[intent.idempotency_key] = result
        return result


@dataclass
class CrashOnce:
    calls: int = 0

    async def __call__(self, _result: PaperOperationResult) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("crash after submit before link")


@pytest.mark.asyncio
async def test_crash_after_submit_recovers_native_truth_without_second_post(
    db_session,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT,
    )
    adapters = {
        Broker.BINANCE: RecoveringAdapter(Broker.BINANCE),
        Broker.ALPACA: RecoveringAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()
    crash = CrashOnce()
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"recovery-run-{nonce}",
        round_decision_id=f"recovery-round-{nonce}",
        mode=RunMode.PAPER_ACTIVE,
    )

    def app_factory(current_verifier):
        return PaperExecutionApplication(
            registry=FakeRegistry(adapters), verifier=current_verifier
        )

    first = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=app_factory,
        native_resolver=native,
        after_submit_hook=crash,
    )
    with pytest.raises(RuntimeError, match="crash after submit"):
        await first.run(invocation)
    await db_session.rollback()
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 1
    assert native.calls == []

    second = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=app_factory,
        native_resolver=native,
        after_submit_hook=crash,
    )
    result = await second.run(invocation)
    await db_session.commit()

    assert result.intent_count == 4
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 4
    assert sum(adapter.replay_count for adapter in adapters.values()) == 1
    assert len(native.calls) == 4
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperRunOrderLink)
            .where(PaperRunOrderLink.run_id == invocation.run_id)
        )
        == 4
    )
