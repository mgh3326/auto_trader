"""ROB-1338 production durable-port factory and restart contracts.

These tests open PostgreSQL only through the repository's test database.  They
never construct a Kiwoom client, invoke a broker callback, or enter the bounded
factory that consumes a one-shot seal.
"""

from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace

import pytest

from app.services.brokers.kiwoom.coordination_store import (
    KiwoomCoordinationStore,
    KiwoomDurableSendClaimAdapter,
    KiwoomRestartClaimState,
)
from app.services.mock_integration.coordination import (
    CoordinationError,
    CoordinationReasonCode,
    DispatchEvidence,
    DispatchEvidenceKind,
    MutationCallbackResult,
    MutationCertainty,
    TerminalClaimEvidence,
    coordinate_mock_order_mutation,
    physical_account_scope_for_entry,
)
from app.services.mock_integration.lineage import MockLineageFactory
from app.services.mock_lane_registry import CANONICAL_LANE_REGISTRY, LaneGuardError
from scripts.b0x.kr import kiwoom_durable_ports
from scripts.b0x.kr.kiwoom_coordination import (
    KIWOOM_KR_LANE_ID,
    production_kiwoom_coordination_factory,
    resolve_kiwoom_lane_entry,
)
from scripts.b0x.kr.kiwoom_ordering import (
    InMemoryDispatchEvidence,
    InMemoryLineagePersistence,
    InMemoryReservationPort,
    InMemoryUncertaintyGate,
    KiwoomCoordinationAdapter,
    KiwoomCoordinationPorts,
)

FACTORY_REFERENCE = "scripts.b0x.kr.kiwoom_durable_ports:build_ports"


def _attempt(ports: KiwoomCoordinationPorts):
    token = uuid.uuid4().hex
    planned = SimpleNamespace(
        symbol="005930",
        side="buy",
        price=50_000,
        quantity=1,
        order_key=f"rob1338-{token}",
        cycle_id=f"rob1338-{token}",
    )
    adapter = KiwoomCoordinationAdapter(ports, grant_only=True)
    return adapter._attempt_envelope(  # noqa: SLF001 - exact lane constructor
        planned,
        policy_version="rob1338-test-v1",
        policy_version_hash="f" * 64,
    )


def _dispatch_evidence(ports, envelope, claim, broker_order_id: str):  # noqa: ANN001
    acknowledged = ports.lineage_factory.acknowledge_order_attempt(
        envelope, broker_order_id
    )
    attempt = acknowledged.order_attempt
    plan = acknowledged.execution_plan
    assert attempt is not None
    assert plan is not None
    return DispatchEvidence(
        envelope=acknowledged,
        kind=DispatchEvidenceKind.ACKNOWLEDGED,
        certainty=MutationCertainty.DEFINITIVE,
        broker_order_id=broker_order_id,
        callback_failed=False,
        ack_attachment_failed=False,
        outer_cancellation_requested=False,
        decision_intent_id=acknowledged.decision_intent.decision_intent_id,
        execution_plan_id=plan.execution_plan_id,
        order_attempt_id=attempt.order_attempt_id,
        cycle_id=attempt.cycle_id,
        attempt_seq=acknowledged.attempt_seq,
        claim_account_scope=claim.claim_account_scope,
        claim_row_id=claim.row_id,
        idempotency_key=claim.idempotency_key,
    )


async def _release_test_claim(ports, claim) -> None:  # noqa: ANN001
    assert (
        await ports.claims.release_with_terminal_evidence(
            claim,
            TerminalClaimEvidence(
                authoritative_absence_proven=True,
                account_position_reconciled=True,
            ),
        )
        == 1
    )


@pytest.mark.unit
def test_module_callable_imports_with_serving_loader_shape_and_returns_exact_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: importlib + vars lookup; factory construction itself performs no I/O."""

    module_name, attribute_name = FACTORY_REFERENCE.split(":", 1)
    module = importlib.import_module(module_name)
    candidate = vars(module).get(attribute_name)
    assert callable(candidate)

    monkeypatch.setattr(
        kiwoom_durable_ports,
        "_new_session",
        lambda: pytest.fail("factory construction opened a database session"),
    )
    entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    ports = candidate(entry)

    assert type(ports) is KiwoomCoordinationPorts
    assert ports.entry is entry
    assert type(ports.persistence) is KiwoomCoordinationStore
    assert ports.dispatch_evidence is ports.persistence
    assert ports.uncertainty_gate is ports.persistence
    assert type(ports.claims) is KiwoomDurableSendClaimAdapter
    assert ports.registry is CANONICAL_LANE_REGISTRY
    assert callable(ports.connection_factory)
    assert type(ports.lineage_factory) is MockLineageFactory
    assert ports.coordination_provenance is not None
    assert ports.legacy_offline is False
    assert not isinstance(ports.persistence, InMemoryLineagePersistence)
    assert not isinstance(ports.dispatch_evidence, InMemoryDispatchEvidence)
    assert not isinstance(ports.uncertainty_gate, InMemoryUncertaintyGate)


@pytest.mark.unit
def test_default_grant_only_factory_never_selects_durable_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: the no-flag production path stays the existing in-memory canary."""

    durable_builds = 0

    def contaminated(_entry):  # noqa: ANN001, ANN202 - adversarial sentinel
        nonlocal durable_builds
        durable_builds += 1
        raise AssertionError("default path selected durable ports")

    monkeypatch.setattr(kiwoom_durable_ports, "build_ports", contaminated)
    owner = production_kiwoom_coordination_factory()()

    assert durable_builds == 0
    assert owner.grant_only is True
    assert type(owner.ports.persistence) is InMemoryLineagePersistence
    assert type(owner.ports.dispatch_evidence) is InMemoryDispatchEvidence
    assert type(owner.ports.uncertainty_gate) is InMemoryUncertaintyGate
    assert isinstance(owner.ports.claims._intents, InMemoryReservationPort)  # noqa: SLF001


@pytest.mark.integration
@pytest.mark.asyncio
async def test_factory_ports_preserve_signed_execution_gate_before_fake_ack(
    db_session,
) -> None:
    """Durable ports do not turn the current NOT_READY registry row into authority."""

    del db_session
    entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    ports = kiwoom_durable_ports.build_ports(entry)
    envelope = _attempt(ports)
    callbacks: list[str] = []
    broker_order_id = f"rob1338-{uuid.uuid4().hex[:16]}"

    async def fake_ack(scope):  # noqa: ANN001, ANN202 - injected fake transport
        await scope.assert_owned()
        callbacks.append("fake_ack")
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE,
            broker_order_id=broker_order_id,
        )

    with pytest.raises(LaneGuardError) as excinfo:
        await coordinate_mock_order_mutation(
            envelope=envelope,
            persistence=ports.persistence,
            dispatch_evidence=ports.dispatch_evidence,
            uncertainty_gate=ports.uncertainty_gate,
            claims=ports.claims,
            connection_factory=ports.connection_factory,
            mutation=fake_ack,
            registry=ports.registry,
            lineage_factory=ports.lineage_factory,
        )
    assert excinfo.value.code == "lane_binding_incomplete"
    assert callbacks == []
    assert await ports.claims.rediscover_unreleased_claims() == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_ack_dispatch_write_is_atomic_for_restart_rediscovery(
    db_session,
) -> None:
    """ACK envelope + typed evidence share one commit; a no-op write is RED."""

    del db_session  # schema/bootstrap dependency; production ports open fresh sessions
    entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    ports = kiwoom_durable_ports.build_ports(entry)
    envelope = _attempt(ports)
    attempt = envelope.order_attempt
    plan = envelope.execution_plan
    assert attempt is not None
    assert plan is not None
    scope = physical_account_scope_for_entry(entry)

    await ports.persistence.persist(envelope)
    claim = await ports.claims.reserve(
        scope=scope,
        idempotency_key=attempt.idempotency_key,
        symbol=plan.normalized_symbol,
        side=envelope.decision_intent.side,
    )
    broker_order_id = f"rob1338-{uuid.uuid4().hex[:16]}"
    evidence = _dispatch_evidence(ports, envelope, claim, broker_order_id)
    try:
        # Deliberately omit persistence.persist(evidence.envelope): the dispatch
        # port must carry both the ACK envelope and typed evidence atomically.
        await ports.dispatch_evidence.persist_dispatch_evidence(evidence)

        restarted = kiwoom_durable_ports.build_ports(entry)
        discovered = await restarted.claims.rediscover_unreleased_claims()
        matching = [row for row in discovered if row.row_id == claim.row_id]
        assert len(matching) == 1
        recovered = matching[0]
        assert recovered.dispatch_kind is DispatchEvidenceKind.ACKNOWLEDGED
        assert recovered.certainty is MutationCertainty.DEFINITIVE
        assert recovered.broker_order_id == broker_order_id
        assert recovered.ack_envelope_recorded is True
        assert recovered.state is KiwoomRestartClaimState.DEFINITIVE_TRACKED
        assert recovered.blocks_account is False
    finally:
        await _release_test_claim(ports, claim)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restart_rediscovers_missing_evidence_claim_and_blocks_account(
    db_session,
) -> None:
    """A crash after claim commit cannot hide the claim or authorize replay."""

    del db_session
    entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    ports = kiwoom_durable_ports.build_ports(entry)
    envelope = _attempt(ports)
    attempt = envelope.order_attempt
    plan = envelope.execution_plan
    assert attempt is not None
    assert plan is not None
    scope = physical_account_scope_for_entry(entry)

    await ports.persistence.persist(envelope)
    claim = await ports.claims.reserve(
        scope=scope,
        idempotency_key=attempt.idempotency_key,
        symbol=plan.normalized_symbol,
        side=envelope.decision_intent.side,
    )
    try:
        restarted = kiwoom_durable_ports.build_ports(entry)
        discovered = await restarted.claims.rediscover_unreleased_claims()
        matching = [row for row in discovered if row.row_id == claim.row_id]
        assert [row.row_id for row in matching] == [claim.row_id]
        assert matching[0].state is KiwoomRestartClaimState.EVIDENCE_MISSING
        assert matching[0].dispatch_kind is None
        assert matching[0].blocks_account is True
        assert (
            await restarted.uncertainty_gate.has_unresolved_account_uncertainty(
                claim_account_scope=scope.claim_account_scope
            )
            is True
        )
        with pytest.raises(CoordinationError) as excinfo:
            await restarted.claims.reserve(
                scope=scope,
                idempotency_key=attempt.idempotency_key,
                symbol=plan.normalized_symbol,
                side=envelope.decision_intent.side,
            )
        assert (
            excinfo.value.reason_code is CoordinationReasonCode.DURABLE_CLAIM_CONFLICT
        )
    finally:
        await _release_test_claim(ports, claim)
