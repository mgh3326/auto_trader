from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.paper_cohort import (
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortTerminalFence,
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
async def test_fresh_and_reloaded_intents_share_persisted_execution_order(
    db_session,
) -> None:
    nonce, activation, _ = await _setup(db_session)
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"ordered-run-{nonce}",
        round_decision_id=f"ordered-round-{nonce}",
        mode=RunMode.SHADOW,
    )
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    cohort, assignments = await runner._cohort(invocation.cohort_id)

    fresh = await runner._prepare(invocation, cohort, assignments)
    fresh_intents = fresh[2]
    await db_session.commit()
    reloaded = await runner._load_prepared(invocation)

    assert reloaded is not None
    reloaded_intents = reloaded[2]
    assert [item[0].intent_id for item in reloaded_intents] == [
        item[0].intent_id for item in fresh_intents
    ]
    assert [item[0].execution_ordinal for item in reloaded_intents] == [0, 1, 2, 3]
    assert [
        (item[0].assignment_id, item[0].symbol, item[0].venue)
        for item in reloaded_intents
    ] == [
        (activation.assignments[0].assignment_id, "BTCUSDT", "binance"),
        (activation.assignments[0].assignment_id, "BTCUSDT", "alpaca"),
        (activation.assignments[0].assignment_id, "ETHUSDT", "binance"),
        (activation.assignments[0].assignment_id, "ETHUSDT", "alpaca"),
    ]


@pytest.mark.asyncio
async def test_prepared_reload_isolated_by_round_when_run_id_is_reused(
    db_session,
) -> None:
    nonce, activation, _ = await _setup(db_session)
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    run_id = f"shared-run-{nonce}"
    invocations = [
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=run_id,
            round_decision_id=f"shared-round-{index}-{nonce}",
            mode=RunMode.SHADOW,
        )
        for index in range(2)
    ]
    cohort, assignments = await runner._cohort(activation.cohort_id)
    for invocation in invocations:
        await runner._prepare(invocation, cohort, assignments)
        await db_session.commit()

    reloaded = await runner._load_prepared(invocations[1])

    assert reloaded is not None
    assert len(reloaded[2]) == 4
    assert {intent.round_decision_id for intent, _signal, _evidence in reloaded[2]} == {
        invocations[1].round_decision_id
    }


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
    uncertain_claim = await db_session.scalar(
        select(PaperCohortRunClaim).where(
            PaperCohortRunClaim.run_id == invocation.run_id
        )
    )
    assert uncertain_claim is not None
    assert uncertain_claim.claim_status == "in_progress"
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
    completed_claim = await db_session.scalar(
        select(PaperCohortRunClaim).where(
            PaperCohortRunClaim.run_id == invocation.run_id
        )
    )
    assert completed_claim is not None
    assert completed_claim.claim_status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_fence", [True, False])
async def test_reconciliation_required_can_resume_with_or_without_fence(
    db_session, monkeypatch, durable_fence: bool
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
            clock=clock,
            enablement=lambda _mode: True,
        ).run(invocation)
    await db_session.rollback()
    live_claim = await db_session.scalar(
        select(PaperCohortRunClaim).where(
            PaperCohortRunClaim.run_id == invocation.run_id
        )
    )
    assert live_claim is not None
    assert live_claim.lease_expires_at > clock()

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
    if durable_fence:
        db_session.add(
            PaperCohortTerminalFence(
                fence_id=f"terminal-recovery-fence-{nonce}",
                cohort_id=activation.cohort_id,
                cohort_hash=activation.expected_cohort_hash,
                idempotency_key=f"terminal-recovery-stop-{nonce}",
                request_hash=stable_hash(f"terminal-recovery-stop-{nonce}"),
                actor_id="operator-1",
                actor_role="operator",
                reason_code="kill_switch",
                reason_text="terminal recovery fence",
                validation_evidence={},
                fenced_at=CAPTURED_AT,
            )
        )
    else:
        # A state mismatch alone does not authorize stealing an unexpired live
        # in-progress claim. Let the original lease expire before recovery.
        clock.now += timedelta(minutes=6)
    await db_session.commit()
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", False)

    with pytest.raises(PaperCohortError, match="reconciliation_required"):
        await PaperCohortRunner(
            db_session,
            capture=capture,
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=app_factory,
            native_resolver=native,
            clock=clock,
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
    assert claim.claim_status == "reconciliation_required"
    assert claim.terminal_reason == "reconciliation_required"
    assert claim.terminal_at is not None
    assert claim.completed_at is None
    assert claim.result_payload is None

    intents = list(
        (
            await db_session.scalars(
                select(PaperCohortVenueIntent).where(
                    PaperCohortVenueIntent.run_id == invocation.run_id
                )
            )
        ).all()
    )
    for prepared_intent in intents:
        native.prepared.setdefault(
            prepared_intent.intent_id,
            NativeOrderIdentity(
                venue=prepared_intent.venue,
                ledger_kind=(
                    "binance_demo_order_ledger"
                    if prepared_intent.venue == "binance"
                    else "alpaca_paper_order_ledger"
                ),
                ledger_row_id=int(
                    stable_hash(f"retry-native-{prepared_intent.intent_id}")[:12], 16
                ),
                client_order_id=(
                    f"retry-client-{prepared_intent.execution_ordinal}-{nonce}"
                ),
                broker_order_id=(
                    f"retry-broker-{prepared_intent.execution_ordinal}-{nonce}"
                ),
            ),
        )
    for prepared_native in native.prepared.values():
        native.identities[(prepared_native.venue, prepared_native.client_order_id)] = (
            prepared_native
        )

    recovered = await PaperCohortRunner(
        db_session,
        capture=capture,
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=app_factory,
        native_resolver=native,
        clock=clock,
        enablement=lambda _mode: True,
    ).recover(invocation)

    assert recovered.intent_count == 4
    assert sum(adapter.broker_posts for adapter in adapters.values()) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperRunOrderLink)
            .where(PaperRunOrderLink.run_id == invocation.run_id)
        )
        == 4
    )
    await db_session.refresh(claim)
    assert claim.claim_status == "completed"
