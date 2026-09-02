"""ROB-1338 production durable-port factory and restart contracts.

These tests open PostgreSQL only through the repository's test database.  They
never construct a Kiwoom client, invoke a broker callback, or enter the bounded
factory that consumes a one-shot seal.
"""

from __future__ import annotations

import importlib
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.kiwoom_authority_cessation import (
    KiwoomAuthorityAttempt,
    KiwoomAuthorityCessationReceipt,
)
from app.services.brokers.kiwoom import coordination_store as store_module
from app.services.brokers.kiwoom.coordination_store import (
    KiwoomCoordinationStore,
    KiwoomDurableSendClaimAdapter,
    KiwoomRestartClaimState,
)
from app.services.mock_integration.authority_cessation import (
    AuthorityAttemptStartedV1,
    AuthorityAttemptTerminalState,
    AuthorityAttemptTerminalV1,
    AuthorityCessationKind,
    AuthorityReleaseStatus,
    terminal_receipt_digest,
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


@pytest.mark.unit
def test_authority_schema_preflight_contract_matches_orm_metadata() -> None:
    """Compiled preflight names must match SQLAlchemy's final convention names."""

    models = {
        "kiwoom_authority_attempts": KiwoomAuthorityAttempt,
        "kiwoom_authority_cessation_receipts": KiwoomAuthorityCessationReceipt,
    }
    for table_name, model in models.items():
        table = model.__table__
        assert (
            tuple(sorted(column.name for column in table.columns))
            == (
                store_module._AUTHORITY_TABLE_COLUMNS[table_name]  # noqa: SLF001
            )
        )
        actual_constraint_names = {constraint.name for constraint in table.constraints}
        expected_constraint_names = set(
            store_module._AUTHORITY_TABLE_CONSTRAINTS[table_name]  # noqa: SLF001
        )
        assert expected_constraint_names.issubset(actual_constraint_names)
        assert all(len(name) <= 63 for name in expected_constraint_names), (
            "PostgreSQL must not silently truncate a preflight constraint name"
        )


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


def _authority_started() -> AuthorityAttemptStartedV1:
    token = uuid.uuid4().hex
    return AuthorityAttemptStartedV1(
        authority_attempt_id=f"authority-attempt:{token}",
        lane_id="kr.kiwoom.mock",
        cycle_id=f"cycle:{token}",
        order_attempt_id=f"order:{token}",
        owner_binding_digest="a" * 64,
        keyset_digest="b" * 64,
        key_count=1,
        baseline_matching_rows=0,
    )


def _authority_terminal(
    started: AuthorityAttemptStartedV1,
) -> AuthorityAttemptTerminalV1:
    draft = AuthorityAttemptTerminalV1(
        authority_attempt_id=started.authority_attempt_id,
        lane_id=started.lane_id,
        cycle_id=started.cycle_id,
        order_attempt_id=started.order_attempt_id,
        claim_row_id=None,
        owner_binding_digest=started.owner_binding_digest,
        keyset_digest=started.keyset_digest,
        key_count=1,
        terminal_state=AuthorityAttemptTerminalState.CESSATION_RECEIPT_COMMITTED,
        kind=AuthorityCessationKind.ADVISORY_UNLOCK,
        lock_statement_dispatched=True,
        lock_definite_false=False,
        acquired_key_count=1,
        in_flight_unknown=False,
        unlock_true_count=1,
        post_release_matching_rows=0,
        termination_returned_exact_true=None,
        observer_pid_absent=None,
        receipt_digest="",
    )
    return replace(draft, receipt_digest=terminal_receipt_digest(draft))


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
    assert ports.authority_evidence is ports.persistence
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mandatory_cancel_blocked_claim_is_picked_up_by_existing_recovery(
    db_session,
) -> None:
    """AC8: uncertain BUY id reaches rediscovery → kt00007 → terminal release."""

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
    broker_order_id = f"rob1340-{uuid.uuid4().hex[:16]}"
    uncertain = replace(
        _dispatch_evidence(ports, envelope, claim, broker_order_id),
        envelope=envelope,
        kind=DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN,
        certainty=MutationCertainty.UNCERTAIN,
    )
    await ports.dispatch_evidence.persist_dispatch_evidence(uncertain)

    restarted_ports = kiwoom_durable_ports.build_ports(entry)
    discovered = await restarted_ports.claims.rediscover_unreleased_claims()
    recovered_claim = next(row for row in discovered if row.row_id == claim.row_id)
    assert recovered_claim.state is KiwoomRestartClaimState.UNCERTAIN
    assert recovered_claim.broker_order_id == broker_order_id
    assert recovered_claim.blocks_account is True

    owner = KiwoomCoordinationAdapter(restarted_ports, grant_only=False)
    resting_disposition = owner.apply_restart_disposition(
        durable_broker_order_id=recovered_claim.broker_order_id,
        kt00007_readable=True,
        kt00007_rows=(
            {
                "order_id": broker_order_id,
                "status": "open",
                "filled_quantity": 0,
                "remaining_quantity": 1,
            },
        ),
    )
    assert resting_disposition.status == "recovered_from_j2b_and_kt00007"
    assert resting_disposition.native is not None
    assert resting_disposition.native.normalized_state == "open"
    assert resting_disposition.native.remaining_quantity == 1
    assert any(
        row.row_id == claim.row_id
        for row in await restarted_ports.claims.rediscover_unreleased_claims()
    ), "a live readback must not release the durable recovery claim"

    # A later terminal kt00007 read (for example broker cancellation or DAY
    # expiry) supplies the existing recovery contract's release evidence.
    disposition = owner.apply_restart_disposition(
        durable_broker_order_id=recovered_claim.broker_order_id,
        kt00007_readable=True,
        kt00007_rows=(
            {
                "order_id": broker_order_id,
                "status": "cancelled",
                "filled_quantity": 0,
                "remaining_quantity": 0,
            },
        ),
    )
    assert disposition.status == "recovered_from_j2b_and_kt00007"
    assert disposition.native is not None
    assert disposition.native.normalized_state == "cancelled"
    assert (
        await owner.release_if_matches_terminal(
            claim,
            TerminalClaimEvidence(
                lane_native_terminal_evidence=True,
                account_position_reconciled=True,
                remainder_known=True,
            ),
        )
        == 1
    )
    assert all(
        row.row_id != claim.row_id
        for row in await restarted_ports.claims.rediscover_unreleased_claims()
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authority_attempt_and_receipt_commit_readback_cover_current_cycle(
    db_session,
) -> None:
    """The append-only DB rows, not cycle JSON, produce RELEASE_VERIFIED."""

    del db_session
    entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    store = kiwoom_durable_ports.build_ports(entry).authority_evidence
    assert isinstance(store, KiwoomCoordinationStore)
    started = _authority_started()
    assert await store.record_started(started) == started
    terminal = _authority_terminal(started)
    committed = await store.record_terminal(terminal)
    assert committed.committed is True
    assert committed.receipt_id is not None

    assessment = await store.release_assessment_for_cycle(cycle_id=started.cycle_id)
    assert assessment.status is AuthorityReleaseStatus.RELEASE_VERIFIED
    assert assessment.committed_receipt_refs == (
        (committed.receipt_id, committed.receipt_digest),
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_name", "operation"),
    (
        ("kiwoom_authority_attempts", "UPDATE"),
        ("kiwoom_authority_attempts", "DELETE"),
        ("kiwoom_authority_attempts", "TRUNCATE"),
        ("kiwoom_authority_cessation_receipts", "UPDATE"),
        ("kiwoom_authority_cessation_receipts", "DELETE"),
        ("kiwoom_authority_cessation_receipts", "TRUNCATE"),
    ),
)
async def test_authority_evidence_tables_reject_every_non_append_mutation(
    db_session, table_name: str, operation: str
) -> None:
    """Attempt and receipt rows reject UPDATE, DELETE, and TRUNCATE."""

    entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    store = kiwoom_durable_ports.build_ports(entry).authority_evidence
    assert isinstance(store, KiwoomCoordinationStore)
    started = _authority_started()
    await store.record_started(started)
    if table_name == "kiwoom_authority_cessation_receipts":
        await store.record_terminal(_authority_terminal(started))

    statements = {
        "UPDATE": (
            f"UPDATE review.{table_name} SET cycle_id = cycle_id "
            "WHERE authority_attempt_id = :attempt_id"
        ),
        "DELETE": (
            f"DELETE FROM review.{table_name} WHERE authority_attempt_id = :attempt_id"
        ),
        "TRUNCATE": (
            f"TRUNCATE TABLE review.{table_name}"
            + (" CASCADE" if table_name == "kiwoom_authority_attempts" else "")
        ),
    }
    parameters = (
        {} if operation == "TRUNCATE" else {"attempt_id": started.authority_attempt_id}
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(text(statements[operation]), parameters)
    await db_session.rollback()
