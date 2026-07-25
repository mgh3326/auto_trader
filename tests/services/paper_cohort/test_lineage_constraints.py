from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortTargetReservation,
    PaperCohortTerminalFence,
    PaperCohortVenueIntent,
)
from app.services.paper_cohort.cohort_service import PaperCohortService
from tests.services.paper_cohort.test_cohort_service import (
    _activation,
    _assignment,
    _authoritative_history,
    _registry_rows,
)
from tests.services.paper_validation.conftest import stable_hash

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Lineage:
    cohort_id: str
    cohort_hash: str
    run_id: str
    round_decision_id: str
    assignment_id: str
    snapshot_id: str
    snapshot_hash: str
    decision_id: str
    intent_id: str
    symbol: str = "BTCUSDT"
    venue: str = "binance"
    execution_ordinal: int = 0


async def _seed_lineage(session: AsyncSession, nonce: str) -> _Lineage:
    experiment, backtest = await _registry_rows(session, nonce)
    assignment = _assignment(experiment, backtest, nonce=nonce)
    activation = _activation((assignment,), nonce=nonce)
    await _authoritative_history(session, activation)
    await PaperCohortService(session).activate(activation)

    run_id = f"paper-run-{nonce}"
    round_decision_id = f"paper-round-{nonce}"
    snapshot_id = f"snapshot-{nonce}"
    snapshot_hash = stable_hash(f"snapshot-{nonce}")
    decision_id = f"decision-{nonce}"
    intent_id = f"intent-{nonce}"
    captured_at = datetime.now(UTC)
    session.add_all(
        [
            CanonicalMarketSnapshot(
                snapshot_id=snapshot_id,
                cohort_id=activation.cohort_id,
                run_id=run_id,
                round_decision_id=round_decision_id,
                schema_id="canonical_market_snapshot.v1",
                source="binance_public_spot",
                host="https://api.binance.com",
                interval="1m",
                required_lookback=30,
                max_capture_skew_ms=2_000,
                max_ticker_age_ms=5_000,
                capture_started_at=captured_at,
                capture_completed_at=captured_at + timedelta(milliseconds=1),
                payload={},
                content_hash=snapshot_hash,
            ),
            PaperCohortDecision(
                decision_id=decision_id,
                cohort_id=activation.cohort_id,
                run_id=run_id,
                round_decision_id=round_decision_id,
                assignment_id=assignment.assignment_id,
                symbol="BTCUSDT",
                snapshot_id=snapshot_id,
                snapshot_hash=snapshot_hash,
                mode="shadow",
                signal_payload={},
                signal_hash=stable_hash(f"signal-{nonce}"),
            ),
            PaperCohortVenueIntent(
                intent_id=intent_id,
                cohort_id=activation.cohort_id,
                run_id=run_id,
                round_decision_id=round_decision_id,
                decision_id=decision_id,
                assignment_id=assignment.assignment_id,
                symbol="BTCUSDT",
                snapshot_id=snapshot_id,
                snapshot_hash=snapshot_hash,
                venue="binance",
                execution_ordinal=0,
                request_payload={},
                request_hash=stable_hash(f"request-{nonce}"),
                venue_quote_evidence={},
                would_order_evidence={},
            ),
        ]
    )
    await session.flush()
    return _Lineage(
        cohort_id=activation.cohort_id,
        cohort_hash=activation.expected_cohort_hash,
        run_id=run_id,
        round_decision_id=round_decision_id,
        assignment_id=assignment.assignment_id,
        snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        decision_id=decision_id,
        intent_id=intent_id,
    )


async def _expect_integrity_error(
    session: AsyncSession,
    statement: str,
    params: dict[str, object],
    constraint_name: str,
) -> None:
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(IntegrityError) as caught:
            await session.execute(text(statement), params)
        assert constraint_name in str(caught.value.orig)
    finally:
        await savepoint.rollback()


async def _expect_db_error(
    session: AsyncSession,
    statement: str,
    *,
    match: str,
    params: dict[str, object] | None = None,
) -> None:
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(DBAPIError, match=match):
            await session.execute(text(statement), params or {})
    finally:
        await savepoint.rollback()


@pytest.mark.asyncio
async def test_composite_lineage_rejects_cross_wired_rows(
    db_session: AsyncSession,
) -> None:
    left = await _seed_lineage(db_session, uuid4().hex)
    right = await _seed_lineage(db_session, uuid4().hex)

    decision_insert = """
        INSERT INTO research.paper_cohort_decisions (
            decision_id, cohort_id, run_id, round_decision_id, assignment_id,
            symbol, snapshot_id, snapshot_hash, mode, signal_payload, signal_hash
        ) VALUES (
            :decision_id, :cohort_id, :run_id, :round_decision_id, :assignment_id,
            :symbol, :snapshot_id, :snapshot_hash, 'shadow', '{}'::jsonb, :signal_hash
        )
    """
    decision_params = {
        "decision_id": f"cross-decision-{uuid4().hex}",
        "cohort_id": left.cohort_id,
        "run_id": left.run_id,
        "round_decision_id": left.round_decision_id,
        "assignment_id": right.assignment_id,
        "symbol": left.symbol,
        "snapshot_id": left.snapshot_id,
        "snapshot_hash": left.snapshot_hash,
        "signal_hash": stable_hash("cross-assignment"),
    }
    await _expect_integrity_error(
        db_session,
        decision_insert,
        decision_params,
        "fk_paper_cohort_decision_assignment_lineage",
    )
    await _expect_integrity_error(
        db_session,
        decision_insert,
        {
            **decision_params,
            "decision_id": f"cross-snapshot-{uuid4().hex}",
            "assignment_id": left.assignment_id,
            "symbol": "ETHUSDT",
            "snapshot_id": right.snapshot_id,
            "snapshot_hash": right.snapshot_hash,
        },
        "fk_paper_cohort_decision_snapshot_lineage",
    )

    await _expect_integrity_error(
        db_session,
        """
        INSERT INTO research.paper_cohort_venue_intents (
            intent_id, cohort_id, run_id, round_decision_id, decision_id,
            assignment_id, symbol, snapshot_id, snapshot_hash, venue,
            execution_ordinal, request_payload, request_hash,
            venue_quote_evidence, would_order_evidence
        ) VALUES (
            :intent_id, :cohort_id, :run_id, :round_decision_id, :decision_id,
            :assignment_id, :symbol, :snapshot_id, :snapshot_hash, :venue,
            1, '{}'::jsonb, :request_hash, '{}'::jsonb, '{}'::jsonb
        )
        """,
        {
            "intent_id": f"cross-intent-{uuid4().hex}",
            "cohort_id": left.cohort_id,
            "run_id": left.run_id,
            "round_decision_id": left.round_decision_id,
            "decision_id": left.decision_id,
            "assignment_id": right.assignment_id,
            "symbol": left.symbol,
            "snapshot_id": left.snapshot_id,
            "snapshot_hash": left.snapshot_hash,
            "venue": "alpaca",
            "request_hash": stable_hash("cross-intent"),
        },
        "fk_paper_cohort_intent_decision_lineage",
    )

    await _expect_integrity_error(
        db_session,
        """
        INSERT INTO research.paper_run_order_links (
            cohort_id, run_id, round_decision_id, intent_id, decision_id,
            assignment_id, symbol, snapshot_id, snapshot_hash, venue,
            native_ledger_kind, native_ledger_row_id, client_order_id,
            broker_order_id
        ) VALUES (
            :cohort_id, :run_id, :round_decision_id, :intent_id, :decision_id,
            :assignment_id, 'ETHUSDT', :snapshot_id, :snapshot_hash, :venue,
            'binance_demo_order_ledger', :native_row, :client_order, :broker_order
        )
        """,
        {
            "cohort_id": left.cohort_id,
            "run_id": left.run_id,
            "round_decision_id": left.round_decision_id,
            "intent_id": left.intent_id,
            "decision_id": left.decision_id,
            "assignment_id": left.assignment_id,
            "snapshot_id": left.snapshot_id,
            "snapshot_hash": left.snapshot_hash,
            "venue": left.venue,
            "native_row": 10_000_000,
            "client_order": f"cross-client-{uuid4().hex}",
            "broker_order": f"cross-broker-{uuid4().hex}",
        },
        "fk_paper_run_order_link_intent_lineage",
    )


@pytest.mark.asyncio
async def test_link_rejects_wrong_venue_native_ledger_pair(
    db_session: AsyncSession,
) -> None:
    lineage = await _seed_lineage(db_session, uuid4().hex)
    await _expect_integrity_error(
        db_session,
        """
        INSERT INTO research.paper_run_order_links (
            cohort_id, run_id, round_decision_id, intent_id, decision_id,
            assignment_id, symbol, snapshot_id, snapshot_hash, venue,
            native_ledger_kind, native_ledger_row_id, client_order_id,
            broker_order_id
        ) VALUES (
            :cohort_id, :run_id, :round_decision_id, :intent_id, :decision_id,
            :assignment_id, :symbol, :snapshot_id, :snapshot_hash, :venue,
            'alpaca_paper_order_ledger', :native_row, :client_order, :broker_order
        )
        """,
        {
            **lineage.__dict__,
            "native_row": 20_000_000,
            "client_order": f"wrong-pair-client-{uuid4().hex}",
            "broker_order": f"wrong-pair-broker-{uuid4().hex}",
        },
        "ck_paper_run_order_link_venue_ledger",
    )


@pytest.mark.asyncio
async def test_reservation_and_terminal_fence_are_append_only(
    db_session: AsyncSession,
) -> None:
    lineage = await _seed_lineage(db_session, uuid4().hex)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            PaperCohortTargetReservation(
                cohort_id=lineage.cohort_id,
                run_id=lineage.run_id,
                round_decision_id=lineage.round_decision_id,
                intent_id=lineage.intent_id,
                decision_id=lineage.decision_id,
                assignment_id=lineage.assignment_id,
                symbol=lineage.symbol,
                snapshot_id=lineage.snapshot_id,
                snapshot_hash=lineage.snapshot_hash,
                venue=lineage.venue,
                execution_ordinal=lineage.execution_ordinal,
            ),
            PaperCohortTerminalFence(
                fence_id=f"fence-{uuid4().hex}",
                cohort_id=lineage.cohort_id,
                cohort_hash=lineage.cohort_hash,
                idempotency_key=f"idempotency-{uuid4().hex}",
                request_hash=stable_hash("fence-request"),
                actor_id="operator-1",
                actor_role="operator",
                reason_code="operator_stop",
                reason_text="operator requested a terminal stop",
                validation_evidence={},
                fenced_at=now,
            ),
        ]
    )
    await db_session.commit()

    await _expect_db_error(
        db_session,
        "UPDATE research.paper_cohort_target_reservations "
        "SET execution_ordinal = execution_ordinal",
        match="append-only",
    )
    await _expect_db_error(
        db_session,
        "DELETE FROM research.paper_cohort_terminal_fences "
        f"WHERE cohort_id = '{lineage.cohort_id}'",
        match="append-only",
    )
    await _expect_db_error(
        db_session,
        "TRUNCATE TABLE research.paper_cohort_target_reservations",
        match="append-only",
    )


@pytest.mark.asyncio
async def test_run_claim_status_defaults_and_state_combinations_are_enforced(
    db_session: AsyncSession,
) -> None:
    lineage = await _seed_lineage(db_session, uuid4().hex)
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    default_status = await db_session.scalar(
        text(
            """
            INSERT INTO research.paper_cohort_run_claims (
                cohort_id, run_id, round_decision_id, request_hash, owner_token,
                lease_expires_at
            ) VALUES (
                :cohort_id, :run_id, :round_decision_id, :request_hash,
                'owner-default', :lease_expires_at
            ) RETURNING claim_status
            """
        ),
        {
            "cohort_id": lineage.cohort_id,
            "run_id": f"claim-default-{uuid4().hex}",
            "round_decision_id": f"claim-round-{uuid4().hex}",
            "request_hash": stable_hash("claim-default"),
            "lease_expires_at": lease_expires_at,
        },
    )
    assert default_status == "in_progress"

    base_params = {
        "cohort_id": lineage.cohort_id,
        "request_hash": stable_hash("claim-invalid"),
        "lease_expires_at": lease_expires_at,
    }
    await _expect_integrity_error(
        db_session,
        """
        INSERT INTO research.paper_cohort_run_claims (
            cohort_id, run_id, round_decision_id, request_hash, owner_token,
            lease_expires_at, claim_status
        ) VALUES (
            :cohort_id, :run_id, :round_decision_id, :request_hash, 'owner',
            :lease_expires_at, 'completed'
        )
        """,
        {
            **base_params,
            "run_id": f"claim-completed-{uuid4().hex}",
            "round_decision_id": f"claim-round-{uuid4().hex}",
        },
        "ck_paper_cohort_run_claim_state_consistency",
    )
    await _expect_integrity_error(
        db_session,
        """
        INSERT INTO research.paper_cohort_run_claims (
            cohort_id, run_id, round_decision_id, request_hash, owner_token,
            lease_expires_at, claim_status
        ) VALUES (
            :cohort_id, :run_id, :round_decision_id, :request_hash, 'owner',
            :lease_expires_at, 'reconciliation_required'
        )
        """,
        {
            **base_params,
            "run_id": f"claim-reconcile-{uuid4().hex}",
            "round_decision_id": f"claim-round-{uuid4().hex}",
        },
        "ck_paper_cohort_run_claim_state_consistency",
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fence_id", " "),
        ("idempotency_key", ""),
        ("actor_id", "\t"),
        ("reason_code", ""),
        ("reason_text", "x" * 1025),
    ],
)
@pytest.mark.asyncio
async def test_terminal_fence_direct_write_rejects_blank_and_oversize_audit_text(
    db_session: AsyncSession,
    column: str,
    value: str,
) -> None:
    lineage = await _seed_lineage(db_session, uuid4().hex)
    values = {
        "fence_id": f"fence-{uuid4().hex}",
        "cohort_id": lineage.cohort_id,
        "cohort_hash": lineage.cohort_hash,
        "idempotency_key": f"idempotency-{uuid4().hex}",
        "request_hash": stable_hash("fence-direct-write"),
        "actor_id": "operator-1",
        "actor_role": "operator",
        "reason_code": "operator_stop",
        "reason_text": "operator requested a terminal stop",
        "fenced_at": datetime.now(UTC),
    }
    values[column] = value
    await _expect_integrity_error(
        db_session,
        """
        INSERT INTO research.paper_cohort_terminal_fences (
            fence_id, cohort_id, cohort_hash, idempotency_key, request_hash,
            actor_id, actor_role, reason_code, reason_text,
            validation_evidence, fenced_at
        ) VALUES (
            :fence_id, :cohort_id, :cohort_hash, :idempotency_key, :request_hash,
            :actor_id, :actor_role, :reason_code, :reason_text,
            '{}'::jsonb, :fenced_at
        )
        """,
        values,
        "ck_paper_cohort_terminal_fence_text_bounds",
    )


@pytest.mark.parametrize(
    ("column", "value", "limit"),
    [
        ("fence_id", "x" * 129, 128),
        ("cohort_id", "x" * 129, 128),
        ("idempotency_key", "x" * 129, 128),
        ("actor_id", "x" * 129, 128),
        ("reason_code", "x" * 65, 64),
    ],
)
@pytest.mark.asyncio
async def test_terminal_fence_direct_write_rejects_oversize_bounded_fields(
    db_session: AsyncSession,
    column: str,
    value: str,
    limit: int,
) -> None:
    lineage = await _seed_lineage(db_session, uuid4().hex)
    values = {
        "fence_id": f"fence-{uuid4().hex}",
        "cohort_id": lineage.cohort_id,
        "cohort_hash": lineage.cohort_hash,
        "idempotency_key": f"idempotency-{uuid4().hex}",
        "request_hash": stable_hash("fence-oversize-direct-write"),
        "actor_id": "operator-1",
        "actor_role": "operator",
        "reason_code": "operator_stop",
        "reason_text": "operator requested a terminal stop",
        "fenced_at": datetime.now(UTC),
    }
    values[column] = value
    await _expect_db_error(
        db_session,
        """
        INSERT INTO research.paper_cohort_terminal_fences (
            fence_id, cohort_id, cohort_hash, idempotency_key, request_hash,
            actor_id, actor_role, reason_code, reason_text,
            validation_evidence, fenced_at
        ) VALUES (
            :fence_id, :cohort_id, :cohort_hash, :idempotency_key, :request_hash,
            :actor_id, :actor_role, :reason_code, :reason_text,
            '{}'::jsonb, :fenced_at
        )
        """,
        params=values,
        match=f"character varying\\({limit}\\)",
    )
