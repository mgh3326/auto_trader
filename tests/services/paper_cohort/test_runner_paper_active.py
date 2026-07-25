from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortTargetReservation,
    PaperCohortTerminalFence,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
    PaperValidationCohortAssignment,
)
from app.models.paper_validation import PaperValidationStateTransition
from app.models.research_backtest import ResearchBacktestRun
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
)
from app.services.paper_cohort.cohort_service import PaperCohortService
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.native_links import NativeOrderIdentity
from app.services.paper_cohort.order_control import PaperCohortOrderControl
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_cohort.signals import CanonicalTargetSignal
from app.services.paper_validation.contracts import ActorRole
from app.services.paper_validation.service import PaperValidationService
from app.services.research_canonical_hash import canonical_sha256
from tests.services.paper_cohort.test_cohort_service import (
    _activation,
    _assignment,
    _authoritative_history,
    _registry_rows,
)
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_cohort.test_runner_shadow import FakeCapture, FakeQuotes
from tests.services.paper_validation.conftest import (
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
    stable_hash,
)

pytestmark = [
    pytest.mark.integration,
    # ROB-968: hold the investment-report cleanup advisory lock during each
    # test. Other workers' helper-session cleanups TRUNCATE the review.* report
    # family (AccessExclusiveLock, FK-propagated); this file's multi-table DML
    # transactions deadlocked against them 3/3 once the ROB-963 rebalance
    # co-scheduled the files (runs 29643108579 / 29643559556). Holding the same
    # advisory lock removes the cross-transaction lock-order cycle by design
    # (see tests/infra/test_schema_barrier.py Task 6).
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


@dataclass
class FakeAdapter:
    broker: Broker
    calls: list[object] = field(default_factory=list)

    async def submit(self, intent):
        self.calls.append(intent)
        suffix = stable_hash(f"{self.broker.value}:{intent.idempotency_key}")[:16]
        return PaperOperationResult(
            operation=PaperOperation.SUBMIT,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code="ok",
            venue=self.broker,
            native_order_id=f"broker-{suffix}",
            native_client_order_id=f"client-{suffix}",
        )

    async def cancel(self, intent):
        self.calls.append(intent)
        return PaperOperationResult(
            operation=PaperOperation.CANCEL,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code="ok",
            venue=self.broker,
            native_order_id="cancelled-order",
            native_client_order_id="cancelled-client",
        )


@dataclass
class FakeRegistry:
    adapters: dict[Broker, FakeAdapter]

    def resolve(self, broker: Broker) -> FakeAdapter:
        return self.adapters[broker]


@dataclass
class FakeNativeResolver:
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    identities: dict[tuple[str, str], NativeOrderIdentity] = field(default_factory=dict)
    prepared: dict[str, NativeOrderIdentity] = field(default_factory=dict)

    async def resolve(
        self, venue: str, client_order_id: str, broker_order_id: str
    ) -> NativeOrderIdentity:
        self.calls.append((venue, client_order_id, broker_order_id))
        key = (venue, client_order_id)
        if key not in self.identities:
            self.identities[key] = NativeOrderIdentity(
                venue=venue,
                ledger_kind=(
                    "binance_demo_order_ledger"
                    if venue == "binance"
                    else "alpaca_paper_order_ledger"
                ),
                ledger_row_id=int(stable_hash(f"{venue}:{client_order_id}")[:12], 16),
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
            )
        return self.identities[key]

    async def resolve_prepared(self, request, provenance) -> NativeOrderIdentity:
        del provenance
        native = self.prepared.get(request.intent_id)
        if native is None:
            raise PaperCohortError("native_order_not_found")
        return native


@dataclass
class FailSecondPreflightVerifier:
    delegate: PaperCohortProvenanceVerifier
    calls: int = 0

    async def verify(self, request):
        self.calls += 1
        if self.calls == 2:
            raise PaperCohortError("provenance_mismatch")
        return await self.delegate.verify(request)


@dataclass
class BlockSecondApplication:
    calls: list[PaperOrderRequest] = field(default_factory=list)

    async def submit(self, request: PaperOrderRequest) -> PaperOperationResult:
        self.calls.append(request)
        if len(self.calls) == 2:
            return PaperOperationResult.blocked(
                operation=PaperOperation.SUBMIT,
                venue=request.venue,
                reason_code="risk_limit_reached",
            )
        suffix = stable_hash(request.intent_id)[:16]
        return PaperOperationResult(
            operation=PaperOperation.SUBMIT,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code="ok",
            venue=request.venue,
            native_order_id=f"broker-{suffix}",
            native_client_order_id=f"client-{suffix}",
        )


@dataclass
class UncertainOutcomeApplication:
    status: PaperOperationStatus
    calls: list[PaperOrderRequest] = field(default_factory=list)

    async def submit(self, request: PaperOrderRequest) -> PaperOperationResult:
        self.calls.append(request)
        return PaperOperationResult(
            operation=PaperOperation.SUBMIT,
            status=self.status,
            reason_code=(
                "provider_error" if self.status is PaperOperationStatus.FAILED else "ok"
            ),
            venue=request.venue,
        )


async def _setup(
    db_session: AsyncSession,
) -> tuple[str, object, PaperValidationService]:
    nonce = uuid4().hex
    experiment, backtest = await _registry_rows(db_session, nonce)
    assignment = _assignment(experiment, backtest, nonce=nonce)
    activation = _activation((assignment,), nonce=nonce)
    activation = activation.model_copy(update={"required_lookback": 3})
    activation = activation.model_copy(
        update={"expected_cohort_hash": activation.computed_cohort_hash()}
    )
    await _authoritative_history(db_session, activation, state="paper_active")
    await PaperCohortService(db_session).activate(activation)
    await db_session.commit()
    validation = PaperValidationService(
        db_session,
        actor_role_provider=FakeActorRoleProvider(
            {"paper-cohort-runner": ActorRole.SYSTEM}
        ),
        frozen_input_provider=FakeFrozenInputHashProvider(assignment.input_hash),
        policy_provider=FakePolicyHashProvider(assignment.policy_hash),
    )
    return nonce, activation, validation


def _app_factory(adapters: dict[Broker, FakeAdapter]):
    def build(verifier):
        return PaperExecutionApplication(
            registry=FakeRegistry(adapters), verifier=verifier
        )

    return build


@pytest.mark.asyncio
async def test_paper_active_submits_via_rob845_and_stores_only_native_links(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(seconds=1),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=native,
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    )

    result = await runner.run(
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"run-{nonce}",
            round_decision_id=f"round-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
    )
    await db_session.commit()

    assert result.intent_count == 4
    assert len(adapters[Broker.BINANCE].calls) == 2
    assert len(adapters[Broker.ALPACA].calls) == 2
    assert len(native.calls) == 4
    links = (
        await db_session.scalars(
            select(PaperRunOrderLink).where(PaperRunOrderLink.run_id == f"run-{nonce}")
        )
    ).all()
    assert len(links) == 4
    assert all(link.snapshot_hash == result.snapshot_hash for link in links)
    assert all(link.client_order_id.startswith("client-") for link in links)
    assert all(link.broker_order_id.startswith("broker-") for link in links)

    verified = adapters[Broker.BINANCE].calls[0]
    original_request = PaperOrderRequest(
        **{name: getattr(verified, name) for name in PaperOrderRequest.model_fields}
    )
    adapter_baseline = len(adapters[Broker.BINANCE].calls)
    for field_name, bad_value in (
        ("cohort_id", "wrong-cohort"),
        ("strategy_hash", "0" * 64),
        ("config_hash", "0" * 64),
        ("policy_hash", "0" * 64),
        ("market_snapshot_hash", "0" * 64),
        ("market_snapshot_source", "wrong-source"),
    ):
        blocked = await PaperExecutionApplication(
            registry=FakeRegistry(adapters), verifier=verifier
        ).submit(original_request.model_copy(update={field_name: bad_value}))
        assert blocked.reason_code == "provenance_verification_failed"
    assert len(adapters[Broker.BINANCE].calls) == adapter_baseline

    intent = await db_session.scalar(
        select(PaperCohortVenueIntent).where(
            PaperCohortVenueIntent.intent_id == original_request.intent_id
        )
    )
    assert intent is not None
    decision = await db_session.scalar(
        select(PaperCohortDecision).where(
            PaperCohortDecision.decision_id == intent.decision_id
        )
    )
    snapshot = await db_session.scalar(
        select(CanonicalMarketSnapshot).where(
            CanonicalMarketSnapshot.snapshot_id == intent.snapshot_id
        )
    )
    assert decision is not None and snapshot is not None
    assignment = await db_session.scalar(
        select(PaperValidationCohortAssignment).where(
            PaperValidationCohortAssignment.assignment_id == decision.assignment_id
        )
    )
    assert assignment is not None
    backtest = await db_session.get(
        ResearchBacktestRun, assignment.source_backtest_run_id
    )
    assert backtest is not None
    original_signal_payload = decision.signal_payload
    original_signal_hash = decision.signal_hash
    original_request_payload = intent.request_payload
    original_request_hash = intent.request_hash
    forged_signal = CanonicalTargetSignal.model_validate(
        original_signal_payload
    ).model_copy(update={"reference_price": "999999"})
    forged_signal = forged_signal.model_copy(
        update={"signal_hash": forged_signal.recomputed_signal_hash()}
    )
    decision.signal_payload = forged_signal.model_dump(mode="json")
    decision.signal_hash = forged_signal.signal_hash
    intent.request_payload = {
        **original_request_payload,
        "signal_hash": forged_signal.signal_hash,
    }
    intent.request_hash = canonical_sha256(intent.request_payload)
    with (
        db_session.no_autoflush,
        pytest.raises(PaperCohortError, match="provenance_mismatch"),
    ):
        await verifier.verify(original_request)
    decision.signal_payload = original_signal_payload
    decision.signal_hash = original_signal_hash
    intent.request_payload = original_request_payload
    intent.request_hash = original_request_hash
    original_snapshot_payload = snapshot.payload
    persisted_mismatches = (
        (intent, "request_hash", intent.request_hash, "0" * 64),
        (decision, "signal_hash", decision.signal_hash, "0" * 64),
        (
            snapshot,
            "payload",
            original_snapshot_payload,
            {**original_snapshot_payload, "content_hash": "0" * 64},
        ),
        (assignment, "strategy_hash", assignment.strategy_hash, "0" * 64),
        (backtest, "trial_status", backtest.trial_status, "rejected"),
    )
    for row, field_name, original, tampered in persisted_mismatches:
        setattr(row, field_name, tampered)
        with db_session.no_autoflush:
            blocked = await PaperExecutionApplication(
                registry=FakeRegistry(adapters), verifier=verifier
            ).submit(original_request)
        assert blocked.reason_code == "provenance_verification_failed"
        assert len(adapters[Broker.BINANCE].calls) == adapter_baseline
        setattr(row, field_name, original)

    original_quote_evidence = intent.venue_quote_evidence
    intent.venue_quote_evidence = {
        **original_quote_evidence,
        "bid_price": "not-a-decimal",
    }
    with (
        db_session.no_autoflush,
        pytest.raises(PaperCohortError, match="provenance_mismatch"),
    ):
        await verifier.verify(original_request)
    intent.venue_quote_evidence = original_quote_evidence


@pytest.mark.asyncio
async def test_non_exact_paper_active_state_blocks_adapter_native_and_links(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    assignment = activation.assignments[0]
    db_session.add(
        PaperValidationStateTransition(
            validation_id=assignment.validation_id,
            validation_version=assignment.validation_version,
            experiment_id=assignment.experiment_id,
            strategy_version_id=assignment.strategy_version_id,
            cohort_id=activation.cohort_id,
            sequence=5,
            idempotency_key=f"promotion-eligible-{nonce}",
            request_hash=stable_hash(f"promotion-eligible-{nonce}"),
            prior_state="paper_active",
            new_state="promotion_eligible",
            actor_id="operator-1",
            actor_role="operator",
            reason_code="test_promotion_gate",
            reason_text="state is authorizable in ROB-848 but not exact paper_active",
            experiment_hash=assignment.experiment_hash,
            cohort_hash=activation.expected_cohort_hash,
            strategy_hash=assignment.strategy_hash,
            config_hash=assignment.config_hash,
            policy_hash=assignment.policy_hash,
            input_hash=assignment.input_hash,
            input_bundle_id="bundle-1",
            policy_version="policy-v1",
            evidence_ids=["gate-1"],
        )
    )
    await db_session.commit()
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(seconds=1),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()
    baseline = await db_session.scalar(
        select(func.count()).select_from(PaperRunOrderLink)
    )
    snapshot_baseline = await db_session.scalar(
        select(func.count()).select_from(CanonicalMarketSnapshot)
    )
    decision_baseline = await db_session.scalar(
        select(func.count()).select_from(PaperCohortDecision)
    )
    intent_baseline = await db_session.scalar(
        select(func.count()).select_from(PaperCohortVenueIntent)
    )
    capture = FakeCapture()
    quotes = FakeQuotes(db_session)
    runner = PaperCohortRunner(
        db_session,
        capture=capture,
        quote_provider=quotes,
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=native,
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    )

    with pytest.raises(PaperCohortError) as exc_info:
        await runner.run(
            CohortRunInvocation(
                cohort_id=activation.cohort_id,
                run_id=f"run-{nonce}",
                round_decision_id=f"round-{nonce}",
                mode=RunMode.PAPER_ACTIVE,
            )
        )
    assert exc_info.value.reason_code == "authoritative_state_mismatch"
    assert adapters[Broker.BINANCE].calls == []
    assert adapters[Broker.ALPACA].calls == []
    assert native.calls == []
    assert capture.calls == []
    assert quotes.calls == []
    assert (
        await db_session.scalar(
            select(func.count()).select_from(CanonicalMarketSnapshot)
        )
        == snapshot_baseline
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(PaperCohortDecision))
        == decision_baseline
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(PaperCohortVenueIntent)
        )
        == intent_baseline
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(PaperRunOrderLink))
        == baseline
    )


@pytest.mark.asyncio
async def test_all_intents_are_preflighted_before_first_adapter_mutation(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    delegate = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT,
    )
    verifier = FailSecondPreflightVerifier(delegate)
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()

    with pytest.raises(PaperCohortError, match="provenance_mismatch"):
        await PaperCohortRunner(
            db_session,
            capture=FakeCapture(),
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=_app_factory(adapters),
            native_resolver=native,
            clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
            enablement=lambda _mode: True,
        ).run(
            CohortRunInvocation(
                cohort_id=activation.cohort_id,
                run_id=f"preflight-run-{nonce}",
                round_decision_id=f"preflight-round-{nonce}",
                mode=RunMode.PAPER_ACTIVE,
            )
        )

    assert verifier.calls == 2
    assert adapters[Broker.BINANCE].calls == []
    assert adapters[Broker.ALPACA].calls == []
    assert native.calls == []


@pytest.mark.asyncio
async def test_later_definitive_block_preserves_prior_link_and_terminalizes_claim(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    application = BlockSecondApplication()
    native = FakeNativeResolver()
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"durable-link-run-{nonce}",
        round_decision_id=f"durable-link-round-{nonce}",
        mode=RunMode.PAPER_ACTIVE,
    )

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortRunner(
            db_session,
            capture=FakeCapture(),
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=lambda _verifier: application,
            native_resolver=native,
            clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        ).run(invocation)
    await db_session.rollback()

    assert exc_info.value.reason_code == "risk_limit_reached"
    assert len(application.calls) == 2
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
            PaperCohortRunClaim.cohort_id == invocation.cohort_id,
            PaperCohortRunClaim.run_id == invocation.run_id,
            PaperCohortRunClaim.round_decision_id == invocation.round_decision_id,
        )
    )
    assert claim is not None
    assert claim.claim_status == "blocked"
    assert claim.terminal_reason == "risk_limit_reached"
    assert claim.terminal_at is not None


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (PaperOperationStatus.FAILED, "provider_error"),
        (PaperOperationStatus.SUCCEEDED, "native_order_identity_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_uncertain_adapter_outcome_keeps_durable_recoverable_claim(
    db_session: AsyncSession,
    status: PaperOperationStatus,
    reason: str,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    application = UncertainOutcomeApplication(status)
    invocation = CohortRunInvocation(
        cohort_id=activation.cohort_id,
        run_id=f"uncertain-run-{status.value}-{nonce}",
        round_decision_id=f"uncertain-round-{status.value}-{nonce}",
        mode=RunMode.PAPER_ACTIVE,
    )

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortRunner(
            db_session,
            capture=FakeCapture(),
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=lambda _verifier: application,
            native_resolver=FakeNativeResolver(),
            clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        ).run(invocation)
    await db_session.rollback()

    assert exc_info.value.reason_code == reason
    assert len(application.calls) == 1
    claim = await db_session.scalar(
        select(PaperCohortRunClaim).where(
            PaperCohortRunClaim.run_id == invocation.run_id
        )
    )
    assert claim is not None
    assert claim.claim_status == "in_progress"
    assert claim.terminal_reason is None
    assert claim.terminal_at is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperCohortTargetReservation)
            .where(PaperCohortTargetReservation.run_id == invocation.run_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_later_round_observes_one_shot_target_without_another_submit(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=FakeNativeResolver(),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    invocations = [
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"one-shot-run-{index}-{nonce}",
            round_decision_id=f"one-shot-round-{index}-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
        for index in range(2)
    ]

    first = await runner.run(invocations[0])
    second = await runner.run(invocations[1])

    assert first.intent_count == second.intent_count == 4
    assert sum(len(adapter.calls) for adapter in adapters.values()) == 4
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperCohortTargetReservation)
            .where(PaperCohortTargetReservation.cohort_id == activation.cohort_id)
        )
        == 4
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperRunOrderLink)
            .where(PaperRunOrderLink.run_id == invocations[1].run_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_terminal_fence_survives_reenable_and_blocks_fresh_submit(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    fence_id = f"fence-{nonce}"
    db_session.add(
        PaperCohortTerminalFence(
            fence_id=fence_id,
            cohort_id=activation.cohort_id,
            cohort_hash=activation.expected_cohort_hash,
            idempotency_key=f"stop-{nonce}",
            request_hash=stable_hash(f"stop-{nonce}"),
            actor_id="operator-1",
            actor_role="operator",
            reason_code="operator_stop",
            reason_text="durable test fence",
            validation_evidence={},
            fenced_at=CAPTURED_AT,
        )
    )
    await db_session.commit()
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    capture = FakeCapture()

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortRunner(
            db_session,
            capture=capture,
            quote_provider=FakeQuotes(db_session),
            verifier=verifier,
            application_factory=_app_factory(adapters),
            native_resolver=FakeNativeResolver(),
            clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        ).run(
            CohortRunInvocation(
                cohort_id=activation.cohort_id,
                run_id=f"fenced-run-{nonce}",
                round_decision_id=f"fenced-round-{nonce}",
                mode=RunMode.PAPER_ACTIVE,
            )
        )

    assert exc_info.value.reason_code == "cohort_stopped"
    assert capture.calls == []
    assert adapters[Broker.BINANCE].calls == []
    assert adapters[Broker.ALPACA].calls == []
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperCohortRunClaim)
            .where(PaperCohortRunClaim.run_id == f"fenced-run-{nonce}")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_fresh_provenance_honors_fence_but_persisted_recovery_is_allowed(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    await PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=FakeNativeResolver(),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
    ).run(
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"verify-fence-run-{nonce}",
            round_decision_id=f"verify-fence-round-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
    )
    submitted = adapters[Broker.BINANCE].calls[0]
    request = PaperOrderRequest(
        **{name: getattr(submitted, name) for name in PaperOrderRequest.model_fields}
    )
    db_session.add(
        PaperCohortTerminalFence(
            fence_id=f"verify-fence-{nonce}",
            cohort_id=activation.cohort_id,
            cohort_hash=activation.expected_cohort_hash,
            idempotency_key=f"verify-stop-{nonce}",
            request_hash=stable_hash(f"verify-stop-{nonce}"),
            actor_id="operator-1",
            actor_role="operator",
            reason_code="operator_stop",
            reason_text="provenance fence test",
            validation_evidence={},
            fenced_at=CAPTURED_AT,
        )
    )
    await db_session.commit()

    with pytest.raises(PaperCohortError) as exc_info:
        await verifier.verify(request)
    persisted = await verifier.verify_persisted(request)

    assert exc_info.value.reason_code == "cohort_stopped"
    assert persisted.intent_id == request.intent_id


@pytest.mark.asyncio
async def test_cancel_and_close_are_limited_to_linked_cohort_capabilities(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT,
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()
    await PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=native,
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    ).run(
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"control-run-{nonce}",
            round_decision_id=f"control-round-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
    )
    links = list(
        (
            await db_session.scalars(
                select(PaperRunOrderLink).where(
                    PaperRunOrderLink.run_id == f"control-run-{nonce}"
                )
            )
        ).all()
    )
    control = PaperCohortOrderControl(
        db_session,
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=native,
    )
    alpaca_link = next(link for link in links if link.venue == "alpaca")
    binance_link = next(link for link in links if link.venue == "binance")
    alpaca_baseline = len(adapters[Broker.ALPACA].calls)
    binance_baseline = len(adapters[Broker.BINANCE].calls)

    canceled = await control.cancel(activation.cohort_id, alpaca_link.id)
    unsupported_cancel = await control.cancel(activation.cohort_id, binance_link.id)
    closed = await control.close(activation.cohort_id, alpaca_link.id)

    assert canceled.operation is PaperOperation.CANCEL
    assert canceled.status is PaperOperationStatus.SUCCEEDED
    assert len(adapters[Broker.ALPACA].calls) == alpaca_baseline + 2
    assert unsupported_cancel.reason_code == "unsupported_capability"
    assert len(adapters[Broker.BINANCE].calls) == binance_baseline
    assert closed.operation is PaperOperation.SUBMIT
    assert closed.status is PaperOperationStatus.SUCCEEDED
    assert adapters[Broker.ALPACA].calls[-1].side == "sell"
    assert (
        adapters[Broker.ALPACA].calls[-1].source_buy_client_order_id
        == alpaca_link.client_order_id
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
                idempotency_key=f"control-abort-{sequence}-{nonce}",
                request_hash=stable_hash(f"control-abort-{sequence}-{nonce}"),
                prior_state=prior_state,
                new_state=new_state,
                actor_id="operator-1",
                actor_role="operator",
                reason_code="kill_switch",
                reason_text="test owned cancel after abort",
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
    terminal_cancel = await control.cancel(activation.cohort_id, alpaca_link.id)
    assert terminal_cancel.status is PaperOperationStatus.SUCCEEDED
    with pytest.raises(PaperCohortError, match="cohort_order_not_owned"):
        await control.cancel("another-cohort", alpaca_link.id)


@pytest.mark.asyncio
async def test_all_owned_rows_are_append_only_after_paper_run(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT,
    )
    native = FakeNativeResolver()
    await PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(
            {
                Broker.BINANCE: FakeAdapter(Broker.BINANCE),
                Broker.ALPACA: FakeAdapter(Broker.ALPACA),
            }
        ),
        native_resolver=native,
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    ).run(
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"immutable-run-{nonce}",
            round_decision_id=f"immutable-round-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
    )
    rows = (
        (
            "paper_validation_cohort_assignments",
            await db_session.scalar(
                select(PaperValidationCohortAssignment.id).where(
                    PaperValidationCohortAssignment.cohort_id == activation.cohort_id
                )
            ),
        ),
        (
            "canonical_market_snapshots",
            await db_session.scalar(
                select(CanonicalMarketSnapshot.id).where(
                    CanonicalMarketSnapshot.run_id == f"immutable-run-{nonce}"
                )
            ),
        ),
        (
            "paper_cohort_decisions",
            await db_session.scalar(
                select(PaperCohortDecision.id).where(
                    PaperCohortDecision.run_id == f"immutable-run-{nonce}"
                )
            ),
        ),
        (
            "paper_cohort_venue_intents",
            await db_session.scalar(
                select(PaperCohortVenueIntent.id).where(
                    PaperCohortVenueIntent.run_id == f"immutable-run-{nonce}"
                )
            ),
        ),
        (
            "paper_run_order_links",
            await db_session.scalar(
                select(PaperRunOrderLink.id).where(
                    PaperRunOrderLink.run_id == f"immutable-run-{nonce}"
                )
            ),
        ),
    )
    await db_session.commit()

    for table_name, row_id in rows:
        assert row_id is not None
        with pytest.raises(DBAPIError, match="append-only"):
            await db_session.execute(
                text(
                    f"UPDATE research.{table_name} SET created_at = created_at "
                    "WHERE id = :row_id"
                ),
                {"row_id": row_id},
            )
            await db_session.commit()
        await db_session.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            await db_session.execute(
                text(f"DELETE FROM research.{table_name} WHERE id = :row_id"),
                {"row_id": row_id},
            )
            await db_session.commit()
        await db_session.rollback()
