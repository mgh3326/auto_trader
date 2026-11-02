"""PostgreSQL acceptance: CI only under the fleet's host safety policy."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.kis_mock_dispatch_evidence import KISMockDispatchEvidence
from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    NativeOrderEvidence,
)
from app.services.kis_mock_runner.singleton import KIS_MOCK_LANE_EVIDENCE_KINDS
from app.services.mock_integration.coordination import (
    DispatchEvidence,
    DispatchEvidenceKind,
    DurableClaim,
    DurableSendClaimAdapter,
    MutationCertainty,
    TerminalClaimEvidence,
    physical_account_scope_for_entry,
)
from app.services.order_send_intent_service import OrderSendIntentService
from tests.services.mock_integration.test_coordination import _attempt_envelope
from tests.services.mock_integration.test_kis_mock_evidence_store import bound_entry

pytestmark = pytest.mark.integration
ODNO = "0007"


@pytest_asyncio.fixture
async def evidence_lane(db_session):
    # Service commits remain inside this test's outer transaction. No migration
    # or broker calls; the test-schema bootstrap creates the model tables in CI.
    connection = await db_session.connection()
    sessions = async_sessionmaker(
        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    entry = bound_entry()
    store = KISMockEvidenceStore(sessions, entry=entry)
    factory, envelope = _attempt_envelope(
        attempt_overrides={"cycle_id": f"kis-evidence-{uuid4().hex}"}
    )
    await store.persist(envelope)
    scope = physical_account_scope_for_entry(entry).claim_account_scope
    async with sessions() as session:
        row_id = await OrderSendIntentService(session).reserve(
            account_scope=scope,
            idempotency_key=envelope.order_attempt.idempotency_key,
            symbol=envelope.decision_intent.symbol,
            side=envelope.decision_intent.side,
        )
    claim = DurableClaim(
        row_id=row_id,
        claim_account_scope=scope,
        idempotency_key=envelope.order_attempt.idempotency_key,
        side=envelope.decision_intent.side,
    )
    yield store, sessions, factory, envelope, claim
    await db_session.rollback()


def dispatch(envelope, claim, *, acknowledged=True):
    return DispatchEvidence(
        envelope=envelope,
        kind=DispatchEvidenceKind.ACKNOWLEDGED
        if acknowledged
        else DispatchEvidenceKind.CALLBACK_FAILED,
        certainty=MutationCertainty.DEFINITIVE
        if acknowledged
        else MutationCertainty.UNCERTAIN,
        broker_order_id=envelope.order_attempt.broker_order_id,
        callback_failed=not acknowledged,
        ack_attachment_failed=False,
        outer_cancellation_requested=False,
        decision_intent_id=envelope.decision_intent.decision_intent_id,
        execution_plan_id=envelope.execution_plan.execution_plan_id,
        order_attempt_id=envelope.order_attempt.order_attempt_id,
        cycle_id=envelope.order_attempt.cycle_id,
        attempt_seq=envelope.attempt_seq,
        claim_account_scope=claim.claim_account_scope,
        claim_row_id=claim.row_id,
        idempotency_key=claim.idempotency_key,
    )


async def ack(store, factory, envelope, claim):
    acknowledged = factory.acknowledge_order_attempt(envelope, ODNO)
    await store.persist(acknowledged)
    await store.persist_dispatch_evidence(dispatch(acknowledged, claim))
    return acknowledged


@pytest.mark.parametrize("kind", KIS_MOCK_LANE_EVIDENCE_KINDS)
async def test_each_kind_round_trips_and_is_idempotent(evidence_lane, kind):
    store, sessions, factory, envelope, claim = evidence_lane
    await ack(store, factory, envelope, claim)
    evidence = NativeOrderEvidence(kind, f"broker-{kind}", ODNO, ODNO)
    await store.record_native_evidence(envelope, evidence)
    await store.record_native_evidence(envelope, evidence)
    async with sessions() as session:
        events = list(
            await session.scalars(
                select(KISMockDispatchEvidence).where(
                    KISMockDispatchEvidence.order_attempt_id
                    == envelope.order_attempt.order_attempt_id
                )
            )
        )
        # One ACK attachment, one dispatch ACK, and one native event: replay
        # never adds a fourth event, independently of the evidence kind.
        assert len(events) == 3
        matching = [
            event
            for event in events
            if event.kind == kind and event.payload.get("readback_order_id") == ODNO
        ]
        assert len(matching) == 1
        assert matching[0].broker_order_id == ODNO
    if kind == "terminal_reconciliation":
        with pytest.raises(KISMockEvidenceConflict, match="immutable_evidence"):
            await store.record_native_evidence(
                envelope,
                replace(
                    evidence,
                    terminal=TerminalClaimEvidence(lane_native_terminal_evidence=True),
                ),
            )


async def test_restart_missing_and_unknown_hold_and_exact_odno_readback(evidence_lane):
    store, sessions, factory, envelope, claim = evidence_lane
    restart = KISMockEvidenceStore(sessions, entry=bound_entry())
    discovered = await restart.rediscover_unreleased_claims()
    own = next(item for item in discovered if item.claim == claim)
    assert own.blocks_account and own.broker_order_id is None
    reader = AsyncMock()
    with pytest.raises(KISMockEvidenceConflict, match="attributed_odno_required"):
        await restart.readback_by_exact_odno(own, reader)
    reader.assert_not_awaited()
    await store.persist_dispatch_evidence(dispatch(envelope, claim, acknowledged=False))
    assert await restart.has_unresolved_account_uncertainty(
        claim_account_scope=claim.claim_account_scope
    )
    # An ACK learned later supplies an exact readback key, but cannot erase the
    # earlier unknown dispatch, or release the surviving claim.
    await store.persist(factory.acknowledge_order_attempt(envelope, ODNO))
    own = next(
        item
        for item in await restart.rediscover_unreleased_claims()
        if item.claim == claim
    )
    reader.return_value = {"odno": "7", "symbol": envelope.decision_intent.symbol}
    with pytest.raises(KISMockEvidenceConflict, match="exact_attributed_readback"):
        await restart.readback_by_exact_odno(own, reader)
    reader.assert_awaited_once_with(ODNO)
    reader.return_value = {"odno": ODNO}
    assert await restart.readback_by_exact_odno(own, reader) == {"odno": ODNO}
    assert own.blocks_account


async def test_release_requires_committed_terminal_and_exact_claim(evidence_lane):
    store, sessions, factory, envelope, claim = evidence_lane
    await ack(store, factory, envelope, claim)
    intents = AsyncMock()
    intents.release_if_matches.return_value = 1
    claims = DurableSendClaimAdapter(intents)
    for kind in ("partial_fill", "cancel", "expiry", "reject"):
        await store.record_native_evidence(
            envelope, NativeOrderEvidence(kind, kind, ODNO, ODNO)
        )
        with pytest.raises(KISMockEvidenceConflict, match="terminal_claim_binding"):
            await store.release_with_recorded_terminal_evidence(
                envelope, broker_evidence_key=kind, claim=claim, claims=claims
            )
    await store.record_native_evidence(
        envelope,
        NativeOrderEvidence("terminal_reconciliation", "incomplete", ODNO, ODNO),
    )
    with pytest.raises(Exception) as error:
        await store.release_with_recorded_terminal_evidence(
            envelope, broker_evidence_key="incomplete", claim=claim, claims=claims
        )
    assert "terminal_evidence_required" in str(error.value)
    intents.release_if_matches.assert_not_awaited()
    terminal = NativeOrderEvidence(
        "terminal_reconciliation",
        "terminal",
        ODNO,
        ODNO,
        TerminalClaimEvidence(
            lane_native_terminal_evidence=True,
            account_position_reconciled=True,
            remainder_known=True,
        ),
    )
    await store.record_native_evidence(envelope, terminal)
    with pytest.raises(KISMockEvidenceConflict, match="terminal_claim_binding"):
        await store.release_with_recorded_terminal_evidence(
            envelope,
            broker_evidence_key="terminal",
            claim=replace(claim, row_id=claim.row_id + 1),
            claims=claims,
        )
    assert (
        await store.release_with_recorded_terminal_evidence(
            envelope, broker_evidence_key="terminal", claim=claim, claims=claims
        )
        == 1
    )
    assert intents.release_if_matches.await_args.kwargs["row_id"] == claim.row_id


async def test_cross_account_and_wrong_odno_never_attach(evidence_lane):
    store, sessions, factory, envelope, claim = evidence_lane
    await ack(store, factory, envelope, claim)
    with pytest.raises(KISMockEvidenceConflict, match="exact_attributed_readback"):
        await store.record_native_evidence(
            envelope, NativeOrderEvidence("cancel", "wrong", "7", "7")
        )
    other = KISMockEvidenceStore(
        sessions,
        entry=replace(bound_entry(), physical_account_id="kismock:v1:" + "b" * 64),
    )
    with pytest.raises(KISMockEvidenceConflict, match="lineage_binding"):
        await other.persist(factory.acknowledge_order_attempt(envelope, ODNO))
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(KISMockDispatchEvidence)
                .where(
                    KISMockDispatchEvidence.order_attempt_id
                    == envelope.order_attempt.order_attempt_id
                )
            )
            == 2
        )


async def test_native_dispatch_key_cannot_clear_missing_dispatch_uncertainty(
    evidence_lane,
):
    store, sessions, factory, envelope, claim = evidence_lane
    # ACK attachment exists, but the coordinator has not persisted its dispatch.
    await store.persist(factory.acknowledge_order_attempt(envelope, ODNO))
    assert await store.has_unresolved_account_uncertainty(
        claim_account_scope=claim.claim_account_scope
    )
    with pytest.raises(KISMockEvidenceConflict, match="reserved_evidence_key"):
        await store.record_native_evidence(
            envelope, NativeOrderEvidence("cancel", "dispatch", ODNO, ODNO)
        )
    own = next(
        item
        for item in await store.rediscover_unreleased_claims()
        if item.claim == claim
    )
    assert own.blocks_account
    assert await store.has_unresolved_account_uncertainty(
        claim_account_scope=claim.claim_account_scope
    )
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(KISMockDispatchEvidence)
                .where(
                    KISMockDispatchEvidence.order_attempt_id
                    == envelope.order_attempt.order_attempt_id
                )
            )
            == 1
        )
    intents = AsyncMock()
    with pytest.raises(KISMockEvidenceConflict, match="terminal_claim_binding"):
        await store.release_with_recorded_terminal_evidence(
            envelope,
            broker_evidence_key="dispatch",
            claim=claim,
            claims=DurableSendClaimAdapter(intents),
        )
    intents.release_if_matches.assert_not_awaited()
