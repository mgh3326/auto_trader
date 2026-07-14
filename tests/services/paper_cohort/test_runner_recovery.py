from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.paper_cohort import (
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
)
from app.models.paper_validation import PaperValidationStateTransition
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
)
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.native_links import NativeOrderIdentity
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


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


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


@dataclass
class AdvancingClock:
    now: datetime = CAPTURED_AT

    def __call__(self) -> datetime:
        return self.now


@dataclass
class ChangingCapture:
    calls: int = 0

    async def capture(self, request):
        self.calls += 1
        captured = await FakeCapture().capture(request)
        if self.calls == 1:
            return captured
        return captured.model_copy(
            update={
                "capture_completed_at": captured.capture_completed_at
                + timedelta(seconds=self.calls),
            }
        )


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
    capture = ChangingCapture()
    clock = AdvancingClock()
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
        capture=capture,
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=app_factory,
        native_resolver=native,
        after_submit_hook=crash,
        clock=clock,
        enablement=lambda _mode: True,
    )
    with pytest.raises(RuntimeError, match="crash after submit"):
        await first.run(invocation)
    await db_session.rollback()
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 1
    assert native.calls == []
    submitted_venue = next(
        venue for venue, adapter in adapters.items() if adapter.broker_posts == 1
    )
    submitted_result = next(iter(adapters[submitted_venue].results.values()))
    submitted_intent = await db_session.scalar(
        select(PaperCohortVenueIntent)
        .join(
            PaperCohortDecision,
            PaperCohortDecision.decision_id == PaperCohortVenueIntent.decision_id,
        )
        .where(
            PaperCohortVenueIntent.run_id == invocation.run_id,
            PaperCohortVenueIntent.venue == submitted_venue.value,
            PaperCohortDecision.symbol == "BTCUSDT",
        )
    )
    assert submitted_intent is not None
    assert submitted_result.native_client_order_id is not None
    assert submitted_result.native_order_id is not None
    native.prepared[submitted_intent.intent_id] = NativeOrderIdentity(
        venue=submitted_venue.value,
        ledger_kind=(
            "binance_demo_order_ledger"
            if submitted_venue is Broker.BINANCE
            else "alpaca_paper_order_ledger"
        ),
        ledger_row_id=int(stable_hash(f"active-native-{nonce}")[:12], 16),
        client_order_id=submitted_result.native_client_order_id,
        broker_order_id=submitted_result.native_order_id,
    )
    clock.now += timedelta(minutes=6)

    second = PaperCohortRunner(
        db_session,
        capture=capture,
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=app_factory,
        native_resolver=native,
        after_submit_hook=crash,
        clock=clock,
        enablement=lambda _mode: True,
    )
    result = await second.run(invocation)
    await db_session.commit()

    assert result.intent_count == 4
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 4
    assert sum(adapter.replay_count for adapter in adapters.values()) == 0
    assert capture.calls == 1
    assert len(native.calls) == 3
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperRunOrderLink)
            .where(PaperRunOrderLink.run_id == invocation.run_id)
        )
        == 4
    )


@pytest.mark.asyncio
async def test_recovery_only_links_native_truth_after_abort_and_kill_switch(
    db_session, monkeypatch
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
    capture = ChangingCapture()
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"terminal-recovery-run-{nonce}",
        round_decision_id=f"terminal-recovery-round-{nonce}",
        mode=RunMode.PAPER_ACTIVE,
    )

    def app_factory(current_verifier):
        return PaperExecutionApplication(
            registry=FakeRegistry(adapters), verifier=current_verifier
        )

    with pytest.raises(RuntimeError, match="crash after submit"):
        await PaperCohortRunner(
            db_session,
            capture=capture,
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=app_factory,
            native_resolver=native,
            after_submit_hook=crash,
            clock=lambda: CAPTURED_AT,
            enablement=lambda _mode: True,
        ).run(invocation)
    await db_session.rollback()

    submitted_venue = next(
        venue for venue, adapter in adapters.items() if adapter.broker_posts == 1
    )
    result = next(iter(adapters[submitted_venue].results.values()))
    intent = await db_session.scalar(
        select(PaperCohortVenueIntent)
        .join(
            PaperCohortDecision,
            PaperCohortDecision.decision_id == PaperCohortVenueIntent.decision_id,
        )
        .where(
            PaperCohortVenueIntent.run_id == invocation.run_id,
            PaperCohortVenueIntent.venue == submitted_venue.value,
            PaperCohortDecision.symbol == "BTCUSDT",
        )
    )
    assert intent is not None
    assert result.native_client_order_id is not None
    assert result.native_order_id is not None
    native.prepared[intent.intent_id] = NativeOrderIdentity(
        venue=submitted_venue.value,
        ledger_kind=(
            "binance_demo_order_ledger"
            if submitted_venue is Broker.BINANCE
            else "alpaca_paper_order_ledger"
        ),
        ledger_row_id=int(stable_hash(f"terminal-native-{nonce}")[:12], 16),
        client_order_id=result.native_client_order_id,
        broker_order_id=result.native_order_id,
    )
    assignment = activation.assignments[0]
    for sequence, prior_state, new_state in (
        (5, "paper_active", "promotion_eligible"),
        (6, "promotion_eligible", "aborted"),
    ):
        db_session.add(
            PaperValidationStateTransition(
                validation_id=assignment.validation_id,
                validation_version=assignment.validation_version,
                experiment_id=assignment.experiment_id,
                strategy_version_id=assignment.strategy_version_id,
                cohort_id=activation.cohort_id,
                sequence=sequence,
                idempotency_key=f"abort-{sequence}-{nonce}",
                request_hash=stable_hash(f"abort-{sequence}-{nonce}"),
                prior_state=prior_state,
                new_state=new_state,
                actor_id="operator-1",
                actor_role="operator",
                reason_code="kill_switch",
                reason_text="test terminal recovery",
                experiment_hash=assignment.experiment_hash,
                cohort_hash=activation.expected_cohort_hash,
                strategy_hash=assignment.strategy_hash,
                config_hash=assignment.config_hash,
                policy_hash=assignment.policy_hash,
                input_hash=assignment.input_hash,
                input_bundle_id="bundle-1",
                policy_version="policy-v1",
                evidence_ids=["kill-switch"],
            )
        )
    await db_session.commit()
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", False)

    with pytest.raises(PaperCohortError, match="recovery_incomplete"):
        await PaperCohortRunner(
            db_session,
            capture=capture,
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=app_factory,
            native_resolver=native,
            clock=lambda: CAPTURED_AT,
            enablement=lambda _mode: True,
        ).recover(invocation)

    assert sum(adapter.broker_posts for adapter in adapters.values()) == 1
    assert sum(adapter.replay_count for adapter in adapters.values()) == 0
    assert capture.calls == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperRunOrderLink)
            .where(PaperRunOrderLink.run_id == invocation.run_id)
        )
        == 1
    )
    claim = await db_session.scalar(
        select(PaperCohortRunClaim).where(
            PaperCohortRunClaim.run_id == invocation.run_id
        )
    )
    assert claim is not None
    assert claim.completed_at is None
    assert claim.result_payload is None
