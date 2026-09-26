"""Real PostgreSQL transitions and an independent fake broker observer."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.brokers.nhplug.client as client_module
import app.services.nhplug_mock.lease_host as lease_host
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.brokers.nhplug.order_evidence import OrderListing, OrderRow
from app.services.nhplug_mock.account_identity import (
    AccountIdentityError,
    KeyMaterial,
    resolve_account_ref,
)
from app.services.nhplug_mock.intent import OrderIntent, body_digest
from app.services.nhplug_mock.ledger import (
    DispatchOutcome,
    LeaseIdentity,
    LedgerConflict,
    NHPlugMockLedger,
)
from app.services.nhplug_mock.outcome import ResponseMeta
from app.services.nhplug_mock.readiness import Stage2Disabled, Stage2Readiness
from app.services.nhplug_mock.transport import Stage2Timing

pytestmark = pytest.mark.integration
READY = Stage2Readiness(True, True, True, True, True)
IDENTITY = LeaseIdentity("test-machine", "test-boot", "test-ns", 1234, 5678)
KEY = KeyMaterial(1, "unit-test-key-id", b"unit-test-key-only")
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/20260926_task711_nhplug_dispatch.py"
)


@pytest_asyncio.fixture(scope="module")
async def nhplug_engine(_bootstrap_test_schema: None) -> AsyncEngine:
    from app.core.db import engine

    spec = importlib.util.spec_from_file_location("nhplug711_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    original_op = migration.op

    def apply(sync_conn: Any, direction: str) -> None:
        migration.op = Operations(MigrationContext.configure(sync_conn))
        getattr(migration, direction)()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(apply, "upgrade")
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(apply, "downgrade")
        migration.op = original_op


@pytest_asyncio.fixture(scope="module")
async def seeded_engine(nhplug_engine: AsyncEngine) -> AsyncEngine:
    async with nhplug_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_key_version(key_version,key_id,key_check) VALUES (1,:id,:check)"
            ),
            {"id": KEY.key_id, "check": KEY.check()},
        )
    return nhplug_engine


async def account(engine: AsyncEngine, suffix: str) -> UUID:
    return await resolve_account_ref(engine, "MOCK-" + suffix, {1: KEY})


async def intent_row(
    engine: AsyncEngine, suffix: str, *, day: date | None = None, price: int = 67400
):
    ref = await account(engine, suffix)
    intent = OrderIntent("place", "buy", "005930", 1, price, None, None, ref)
    ledger = NHPlugMockLedger(engine)
    row, should_claim = await ledger.create_intent(
        intent,
        readiness=READY,
        idempotency_key="key_" + suffix + "_1234567890",
        order_date=day or date.today(),
    )
    assert should_claim
    return ledger, row, ref


def listing(number: int, *, symbol: str = "005930", price: int = 67400) -> OrderListing:
    return OrderListing(
        "all",
        True,
        (OrderRow(number, symbol, 1, 0, 1, side="buy", order_price=Decimal(price)),),
        pages=1,
        response_codes=("00000",),
    )


class FakeBroker:
    """Request observer independent of the client and ledger code."""

    def __init__(self, *, number: str = "123", close_error: bool = False) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.number = number
        self.close_error = close_error

    def transport(self) -> httpx.AsyncBaseTransport:
        observer = self

        class Wire(httpx.AsyncBaseTransport):
            def arm(self, deadline: float) -> None:
                assert deadline > 0

            async def hard_close(self, timeout: float) -> None:
                if observer.close_error:
                    raise OSError("injected close failure")

            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                observer.requests.append(
                    (request.url.path, json.loads(request.content))
                )
                return httpx.Response(
                    200,
                    json={
                        "rsp_cd": "00000",
                        "Output_0": {"mkt_orr_no": observer.number},
                    },
                    request=request,
                )

        return Wire()


async def client_for(suffix: str) -> NHPlugMockClient:
    async def token() -> str:
        return "unit-test-token"

    read_wire = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "rsp_cd": "00000",
                "Output_0": [{"acct_no": "MOCK-" + suffix, "acct_type": "03"}],
            },
            request=request,
        )
    )
    client = NHPlugMockClient(
        app_key="test", app_secret="test", token_provider=token, transport=read_wire
    )
    await client.verify_and_bind_mock_account("MOCK-" + suffix)
    return client


@pytest.fixture(autouse=True)
def mock_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    for name in ("KEY", "TIME", "DB", "HOST", "VENDOR"):
        monkeypatch.setenv(f"NHPLUG_STAGE2_{name}_CONFIRMED", "true")


@pytest.mark.asyncio
async def test_minimal_path_fake_send_uncertain_then_own_number_reconcile(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "minimal")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("minimal")
    result = await client.dispatch_claimed_order(
        ledger,
        row["id"],
        row["client_request_id"],
        row["body_digest"],
        ref,
        keys={1: KEY},
        identity=IDENTITY,
        readiness=READY,
        timing=Stage2Timing(),
        dry_run=False,
        confirm=True,
    )
    assert result == DispatchOutcome("uncertain", "no_proof_code", "123")
    assert broker.requests == [
        (
            "/krstock/order/v1/cashBuy",
            {
                "Input_0": {
                    "act_no": "MOCK-minimal",
                    "iem_cd": "005930",
                    "orr_qty": 1,
                    "orr_pr": 67400,
                    "nmn_pr_tp_cd": "01",
                    "orr_cnd_dit_cd": "00",
                    "ssl_nmn_pr_dit_cd": "00",
                    "rmt_mkt_cd": "KRX",
                    "sor_mkt_sli_yn": "N",
                }
            },
        )
    ]
    middle = await ledger.get(row["id"])
    assert (
        middle is not None
        and middle["state"] == "uncertain"
        and middle["ack_evidence_order_id"] == "123"
    )
    assert middle["dispatcher_done_at"] is not None
    assert await ledger.verify_own_number(row["id"], listing(123), readiness=READY)
    final = await ledger.get(row["id"])
    assert (
        final is not None
        and final["state"] == "accepted"
        and final["broker_order_id"] == "123"
    )
    assert final["ack_order_id"] == "123"


@pytest.mark.asyncio
async def test_date_independent_reservation_and_direct_release_guards(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "reserve")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    async with seeded_engine.begin() as conn:
        with pytest.raises(Exception, match="no-order proof code absent"):
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='rejected', reject_rsp_cd='arbitrary', "
                    "lease_closed_at=now(), dispatcher_done_at=now() WHERE id=:id"
                ),
                {"id": row["id"]},
            )
    assert (await ledger.get(row["id"]))["state"] == "sending"
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code")
    )
    async with seeded_engine.begin() as conn:
        with pytest.raises(
            Exception, match="anomaly evidence absent|positive listing proof absent"
        ):
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='anomaly', "
                    "requires_manual_review=true, manual_review_reason='invented' WHERE id=:id"
                ),
                {"id": row["id"]},
            )
    assert (await ledger.get(row["id"]))["state"] == "uncertain"
    with pytest.raises(LedgerConflict, match="in_flight_order_exists"):
        await ledger.create_intent(
            OrderIntent("place", "buy", "005930", 1, 67500, None, None, ref),
            readiness=READY,
            idempotency_key="another_key_reserve_12345",
            order_date=date.today() + timedelta(days=1),
        )


@pytest.mark.asyncio
async def test_result_write_failure_leaves_sending_then_recovery_uncertain(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "resultfailure")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim, lease_seconds=1)
    # A lost result commit is not evidence of no send. The exact immediate state is sending.
    immediate = await ledger.get(row["id"])
    assert immediate is not None and immediate["state"] == "sending"
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await ledger.claim(
            row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
        )
    await asyncio.sleep(1.1)
    assert (await ledger.recover_expired())[2] >= 1
    later = await ledger.get(row["id"])
    assert (
        later is not None
        and later["state"] == "uncertain"
        and later["lease_closed_at"] is not None
    )


@pytest.mark.asyncio
async def test_account_key_mismatch_has_zero_writes(seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    with pytest.raises(AccountIdentityError, match="key_mismatch"):
        await resolve_account_ref(
            seeded_engine,
            "MOCK-key-mismatch",
            {1: KeyMaterial(1, KEY.key_id, b"different-test-key")},
        )
    async with seeded_engine.connect() as conn:
        after = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_ack_is_extracted_before_metadata_failure(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "metadata")
    broker = FakeBroker(number="321")
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    monkeypatch.setattr(
        ResponseMeta,
        "of",
        classmethod(lambda cls, response: (_ for _ in ()).throw(ValueError("meta"))),
    )
    client = await client_for("metadata")
    result = await client.dispatch_claimed_order(
        ledger,
        row["id"],
        row["client_request_id"],
        row["body_digest"],
        ref,
        keys={1: KEY},
        identity=IDENTITY,
        readiness=READY,
        timing=Stage2Timing(),
        dry_run=False,
        confirm=True,
    )
    assert len(broker.requests) == 1
    assert result.state == "uncertain" and result.evidence_order_id == "321"
    assert (await ledger.get(row["id"]))["ack_evidence_order_id"] == "321"


@pytest.mark.asyncio
async def test_close_failure_preserves_ack_and_no_second_send(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "close")
    broker = FakeBroker(number="456", close_error=True)
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("close")
    result = await client.dispatch_claimed_order(
        ledger,
        row["id"],
        row["client_request_id"],
        row["body_digest"],
        ref,
        keys={1: KEY},
        identity=IDENTITY,
        readiness=READY,
        timing=Stage2Timing(),
        dry_run=False,
        confirm=True,
    )
    assert result.state == "uncertain" and result.evidence_order_id == "456"
    assert (await ledger.get(row["id"]))["ack_evidence_order_id"] == "456"
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await client.dispatch_claimed_order(
            ledger,
            row["id"],
            row["client_request_id"],
            row["body_digest"],
            ref,
            keys={1: KEY},
            identity=IDENTITY,
            readiness=READY,
            timing=Stage2Timing(),
            dry_run=False,
            confirm=True,
        )
    assert len(broker.requests) == 1


@pytest.mark.asyncio
async def test_cancelled_send_records_uncertain_before_reraising(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "cancelled-send")
    started = asyncio.Event()
    recording = asyncio.Event()
    resume_recording = asyncio.Event()
    observed: list[str] = []
    original_record = ledger.record_final

    async def held_record(claim: Any, outcome: DispatchOutcome) -> bool:
        recording.set()
        await resume_recording.wait()
        return await original_record(claim, outcome)

    monkeypatch.setattr(ledger, "record_final", held_record)

    class WaitingWire(httpx.AsyncBaseTransport):
        def arm(self, deadline: float) -> None:
            assert deadline > 0

        async def hard_close(self, timeout: float) -> None:
            return None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            observed.append(request.url.path)
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    monkeypatch.setattr(client_module, "GatedTransport", WaitingWire)
    client = await client_for("cancelled-send")
    task = asyncio.create_task(
        client.dispatch_claimed_order(
            ledger,
            row["id"],
            row["client_request_id"],
            row["body_digest"],
            ref,
            keys={1: KEY},
            identity=IDENTITY,
            readiness=READY,
            timing=Stage2Timing(),
            dry_run=False,
            confirm=True,
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.wait_for(recording.wait(), 5)
    task.cancel()
    resume_recording.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    stored = await ledger.get(row["id"])
    assert (
        stored is not None
        and stored["state"] == "uncertain"
        and stored["dispatcher_done_at"] is not None
    )
    assert observed == ["/krstock/order/v1/cashBuy"]


@pytest.mark.asyncio
async def test_order_number_unique_per_account_and_trading_day(
    seeded_engine: AsyncEngine,
) -> None:
    for suffix in ("number-a", "number-b"):
        ledger, row, ref = await intent_row(seeded_engine, suffix)
        claim = await ledger.claim(
            row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
        )
        assert await ledger.fence(claim)
        assert await ledger.record_final(
            claim, DispatchOutcome("uncertain", "no_proof_code", "999")
        )
        assert await ledger.verify_own_number(row["id"], listing(999), readiness=READY)
        assert (await ledger.get(row["id"]))["broker_order_id"] == "999"


@pytest.mark.asyncio
async def test_same_account_order_number_collides_only_within_trading_day(
    seeded_engine: AsyncEngine,
) -> None:
    ref = await account(seeded_engine, "number-scope")
    ledger = NHPlugMockLedger(seeded_engine)

    async def accept(symbol: str, day: date, key: str) -> dict[str, Any]:
        intent = OrderIntent("place", "buy", symbol, 1, 67400, None, None, ref)
        row, should_claim = await ledger.create_intent(
            intent,
            readiness=READY,
            idempotency_key=key,
            order_date=day,
        )
        assert should_claim
        claim = await ledger.claim(
            row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
        )
        assert await ledger.fence(claim)
        assert await ledger.record_final(
            claim, DispatchOutcome("uncertain", "no_proof_code", "999")
        )
        return row

    today = date.today()
    first = await accept("005930", today, "number_scope_first_1234")
    assert await ledger.verify_own_number(first["id"], listing(999), readiness=READY)
    same_day = await accept("000660", today, "number_scope_second_123")
    with pytest.raises(IntegrityError):
        await ledger.verify_own_number(
            same_day["id"], listing(999, symbol="000660"), readiness=READY
        )
    assert (await ledger.get(same_day["id"]))["state"] == "uncertain"
    next_day = await accept(
        "035420", today + timedelta(days=1), "number_scope_third_1234"
    )
    assert await ledger.verify_own_number(
        next_day["id"], listing(999, symbol="035420"), readiness=READY
    )
    assert (await ledger.get(next_day["id"]))["state"] == "accepted"


@pytest.mark.asyncio
async def test_migration_matches_orm_columns_indexes_triggers_functions_and_role(
    seeded_engine: AsyncEngine,
) -> None:
    from app.models.nhplug_mock_dispatch import (
        NHPlugMockAccountBinding,
        NHPlugMockAccountRef,
        NHPlugMockKeyVersion,
        NHPlugMockOperatorAuthorization,
        NHPlugMockOrderLedger,
        NHPlugNoOrderProofCode,
        NHPlugSuccessProofCode,
    )

    models = (
        NHPlugMockAccountBinding,
        NHPlugMockAccountRef,
        NHPlugMockKeyVersion,
        NHPlugMockOperatorAuthorization,
        NHPlugMockOrderLedger,
        NHPlugNoOrderProofCode,
        NHPlugSuccessProofCode,
    )
    async with seeded_engine.connect() as conn:
        for model in models:
            columns = (
                await conn.execute(
                    text(
                        "SELECT column_name,is_nullable FROM information_schema.columns "
                        "WHERE table_schema='review' AND table_name=:name"
                    ),
                    {"name": model.__tablename__},
                )
            ).all()
            actual = {name: nullable == "YES" for name, nullable in columns}
            mapped = {
                column.name: column.nullable for column in model.__table__.columns
            }
            assert actual == mapped, model.__tablename__
        indexes = (
            await conn.execute(
                text(
                    "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='review' "
                    "AND tablename='nhplug_mock_order_ledger'"
                )
            )
        ).all()
        definitions = dict(indexes)
        assert (
            "(account_ref, order_date, broker_order_id)"
            in definitions["uq_nhplug_mock_order_number"]
        )
        assert (
            "(account_ref, symbol, side)"
            in definitions["uq_nhplug_mock_active_reservation"]
        )
        assert "order_date" not in definitions["uq_nhplug_mock_active_reservation"]
        trigger_names = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid='review.nhplug_mock_order_ledger'::regclass "
                        "AND NOT tgisinternal"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {"nhplug_order_guard", "nhplug_order_no_truncate"} <= trigger_names
        functions = set(
            (
                await conn.execute(
                    text(
                        "SELECT proname FROM pg_proc JOIN pg_namespace ON pg_namespace.oid=pg_proc.pronamespace "
                        "WHERE nspname='review' AND proname LIKE 'nhplug_%'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {
            "nhplug_order_guard",
            "nhplug_body_digest_v1",
            "nhplug_consume_authorization",
        } <= functions
        owners = (
            await conn.execute(
                text(
                    "SELECT relname,pg_get_userbyid(relowner) FROM pg_class WHERE relnamespace='review'::regnamespace "
                    "AND relname IN ('nhplug_mock_operator_authorization','nhplug_success_proof_code',"
                    "'nhplug_no_order_proof_code','nhplug_mock_key_version')"
                )
            )
        ).all()
        assert len(owners) == 4 and all(
            owner == "nhplug_operator" for _, owner in owners
        )


@pytest.mark.asyncio
async def test_digest_golden_vector_and_db_column(seeded_engine: AsyncEngine) -> None:
    ref = UUID("3f2b8c1e-7a4d-4e6b-9c0a-5d1e2f3a4b5c")
    assert body_digest(
        OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref)
    ) == ("a4793578393129f350e1c6c8ebf6b014f4f365a44b112a4153a2a9e8c4b2a416")
    ledger, row, _ = await intent_row(seeded_engine, "digest")
    assert row["body_digest"] == body_digest(
        OrderIntent("place", "buy", "005930", 1, 67400, None, None, row["account_ref"])
    )


@pytest.mark.asyncio
async def test_two_independent_sessions_claim_once(seeded_engine: AsyncEngine) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "race")

    async def race() -> bool:
        try:
            await ledger.claim(
                row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
            )
            return True
        except LedgerConflict as exc:
            assert exc.code == "claim_rejected"
            return False

    assert sorted(await asyncio.gather(race(), race())) == [False, True]
    assert (await ledger.get(row["id"]))["state"] == "claimed"


@pytest.mark.asyncio
async def test_app_role_can_read_but_cannot_create_authority(
    seeded_engine: AsyncEngine,
) -> None:
    role = "nhplug_test_app_" + uuid4().hex[:12]
    async with seeded_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA review TO {role}"))
        await conn.execute(
            text(
                f"GRANT SELECT ON review.nhplug_mock_operator_authorization, "
                f"review.nhplug_success_proof_code, review.nhplug_no_order_proof_code, "
                f"review.nhplug_mock_key_version TO {role}"
            )
        )
    try:
        async with seeded_engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM review.nhplug_mock_key_version")
                )
            ).scalar_one() == 1
            for table in (
                "nhplug_mock_operator_authorization",
                "nhplug_success_proof_code",
                "nhplug_no_order_proof_code",
                "nhplug_mock_key_version",
            ):
                with pytest.raises(Exception, match="permission denied"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(f"INSERT INTO review.{table} DEFAULT VALUES")
                        )
    finally:
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    f"REVOKE ALL ON review.nhplug_mock_operator_authorization, "
                    f"review.nhplug_success_proof_code, review.nhplug_no_order_proof_code, "
                    f"review.nhplug_mock_key_version FROM {role}"
                )
            )
            await conn.execute(text(f"REVOKE USAGE ON SCHEMA review FROM {role}"))
            await conn.execute(text(f"DROP ROLE {role}"))


@pytest.mark.asyncio
async def test_unsupported_anomaly_keeps_reservation_but_guard_mutant_is_assertion_red(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "anomaly-mutant")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code")
    )
    assert (await ledger.get(row["id"]))["state"] == "uncertain"
    async with seeded_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "ALTER TABLE review.nhplug_mock_order_ledger DISABLE TRIGGER nhplug_order_guard"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='anomaly', "
                    "manual_review_reason='mutant' WHERE id=:id"
                ),
                {"id": row["id"]},
            )
            state = (
                await conn.execute(
                    text(
                        "SELECT state FROM review.nhplug_mock_order_ledger WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
            ).scalar_one()
            with pytest.raises(AssertionError):
                assert state == "uncertain"
        finally:
            await trans.rollback()
    assert (await ledger.get(row["id"]))["state"] == "uncertain"


@pytest.mark.asyncio
async def test_no_order_code_guard_mutant_is_assertion_red(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "reject-mutant")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    async with seeded_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "ALTER TABLE review.nhplug_mock_order_ledger DISABLE TRIGGER nhplug_order_guard"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='rejected', "
                    "reject_rsp_cd='arbitrary', lease_closed_at=now(), dispatcher_done_at=now() "
                    "WHERE id=:id"
                ),
                {"id": row["id"]},
            )
            state = (
                await conn.execute(
                    text(
                        "SELECT state FROM review.nhplug_mock_order_ledger WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
            ).scalar_one()
            with pytest.raises(AssertionError):
                assert state == "sending"
        finally:
            await trans.rollback()
    assert (await ledger.get(row["id"]))["state"] == "sending"


@pytest.mark.asyncio
async def test_date_reservation_index_mutant_is_assertion_red(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "date-mutant")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code")
    )
    async with seeded_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text("DROP INDEX review.uq_nhplug_mock_active_reservation")
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_nhplug_mock_active_reservation ON "
                    "review.nhplug_mock_order_ledger(account_ref, order_date, symbol, side) "
                    "WHERE state IN ('intent','claimed','sending','uncertain')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_order_ledger "
                    "(client_request_id,account_ref,idempotency_key,attempt_no,order_date,operation_kind,"
                    "side,symbol,quantity,price) VALUES (:request,:acct,:key,1,:day,'place','buy','005930',1,67500)"
                ),
                {
                    "request": uuid4(),
                    "acct": ref,
                    "key": "mutant_next_day_123456",
                    "day": date.today() + timedelta(days=1),
                },
            )
            active = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM review.nhplug_mock_order_ledger WHERE account_ref=:acct "
                        "AND symbol='005930' AND side='buy' AND state IN ('intent','claimed','sending','uncertain')"
                    ),
                    {"acct": ref},
                )
            ).scalar_one()
            with pytest.raises(AssertionError):
                assert active == 1
        finally:
            await trans.rollback()
    assert (await ledger.get(row["id"]))["state"] == "uncertain"


@pytest.mark.asyncio
async def test_wrong_key_check_mutant_is_assertion_red(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = KeyMaterial(1, KEY.key_id, b"wrong-test-key")
    async with seeded_engine.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    good_check = KEY.check()
    monkeypatch.setattr(KeyMaterial, "check", lambda self: good_check)
    # The removed check lets a different physical key register a second identity.
    await resolve_account_ref(seeded_engine, "MOCK-wrong-key-mutant", {1: wrong})
    async with seeded_engine.connect() as conn:
        after = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    with pytest.raises(AssertionError):
        assert after == before


@pytest.mark.asyncio
async def test_stage2_confirmation_blocks_t1_and_dispatch_before_claim(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = await account(seeded_engine, "gate")
    ledger = NHPlugMockLedger(seeded_engine)
    intent = OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref)
    monkeypatch.delenv("NHPLUG_STAGE2_VENDOR_CONFIRMED")
    with pytest.raises(Stage2Disabled, match="vendor_unconfirmed"):
        await ledger.create_intent(
            intent,
            readiness=READY,
            idempotency_key="gate_123456789012345",
            order_date=date.today(),
        )
    async with seeded_engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM review.nhplug_mock_order_ledger WHERE account_ref=:ref"
                ),
                {"ref": ref},
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_idempotent_resume_withdrawn_retry_and_body_conflict(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "idem")
    intent = OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref)
    key = row["idempotency_key"]
    resumed, should_claim = await ledger.create_intent(
        intent, readiness=READY, idempotency_key=key, order_date=date.today()
    )
    assert resumed["id"] == row["id"] and should_claim is True
    with pytest.raises(LedgerConflict, match="idempotency_key_conflict"):
        await ledger.create_intent(
            OrderIntent("place", "buy", "005930", 1, 67500, None, None, ref),
            readiness=READY,
            idempotency_key=key,
            order_date=date.today(),
        )
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.withdraw(claim, "pre_send_refusal")
    retried, should_claim = await ledger.create_intent(
        intent, readiness=READY, idempotency_key=key, order_date=date.today()
    )
    assert retried["id"] != row["id"] and retried["attempt_no"] == 2 and should_claim
    claim2 = await ledger.claim(
        retried["id"],
        retried["client_request_id"],
        retried["body_digest"],
        ref,
        IDENTITY,
    )
    assert await ledger.fence(claim2)
    returned, should_claim = await ledger.create_intent(
        intent, readiness=READY, idempotency_key=key, order_date=date.today()
    )
    assert returned["id"] == retried["id"] and should_claim is False


@pytest.mark.asyncio
async def test_same_key_concurrent_intent_creation_has_one_row(
    seeded_engine: AsyncEngine,
) -> None:
    ref = await account(seeded_engine, "idem-race")
    ledger = NHPlugMockLedger(seeded_engine)
    intent = OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref)

    async def create() -> tuple[dict[str, Any], bool]:
        return await ledger.create_intent(
            intent,
            readiness=READY,
            idempotency_key="idem_race_123456789",
            order_date=date.today(),
        )

    first, second = await asyncio.gather(create(), create())
    assert first[0]["id"] == second[0]["id"]
    async with seeded_engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM review.nhplug_mock_order_ledger WHERE account_ref=:ref"
                ),
                {"ref": ref},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_key_rotation_preserves_ref_and_inflight_reservation(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "rotation")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", "789")
    )
    second = KeyMaterial(2, "unit-test-key-v2", b"second-unit-test-key")
    async with seeded_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_key_version(key_version,key_id,key_check) "
                "VALUES (2,:id,:check)"
            ),
            {"id": second.key_id, "check": second.check()},
        )
    try:
        resolved = await resolve_account_ref(
            seeded_engine, "MOCK-rotation", {1: KEY, 2: second}
        )
        assert resolved == ref
        async with seeded_engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM review.nhplug_mock_account_binding WHERE account_ref=:ref"
                    ),
                    {"ref": ref},
                )
            ).scalar_one()
        assert count == 2
        resumed, should_claim = await ledger.create_intent(
            OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref),
            readiness=READY,
            idempotency_key=row["idempotency_key"],
            order_date=date.today(),
        )
        assert resumed["id"] == row["id"] and not should_claim
        with pytest.raises(AccountIdentityError, match="key_version_unavailable"):
            await resolve_account_ref(seeded_engine, "MOCK-rotation", {1: KEY})
        with pytest.raises(LedgerConflict, match="in_flight_order_exists"):
            await ledger.create_intent(
                OrderIntent("place", "buy", "005930", 1, 67500, None, None, ref),
                readiness=READY,
                idempotency_key="rotation_new_123456789",
                order_date=date.today() + timedelta(days=1),
            )
    finally:
        # Disposable test schema cleanup only; production key versions are append-only.
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE review.nhplug_mock_account_binding DISABLE TRIGGER nhplug_binding_immutable"
                )
            )
            await conn.execute(
                text(
                    "DELETE FROM review.nhplug_mock_account_binding WHERE key_version=2"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE review.nhplug_mock_account_binding ENABLE TRIGGER nhplug_binding_immutable"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE review.nhplug_mock_key_version DISABLE TRIGGER nhplug_key_immutable"
                )
            )
            await conn.execute(
                text("DELETE FROM review.nhplug_mock_key_version WHERE key_version=2")
            )
            await conn.execute(
                text(
                    "ALTER TABLE review.nhplug_mock_key_version ENABLE TRIGGER nhplug_key_immutable"
                )
            )


@pytest.mark.parametrize(
    (
        "operation",
        "side",
        "quantity",
        "price",
        "original",
        "scope",
        "expected_path",
        "expected_input",
    ),
    [
        (
            "place",
            "buy",
            1,
            67400,
            None,
            None,
            "/krstock/order/v1/cashBuy",
            {
                "iem_cd": "005930",
                "orr_qty": 1,
                "orr_pr": 67400,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
        ),
        (
            "place",
            "sell",
            3,
            71000,
            None,
            None,
            "/krstock/order/v1/cashSell",
            {
                "iem_cd": "005930",
                "orr_qty": 3,
                "orr_pr": 71000,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
        ),
        (
            "modify",
            "buy",
            1,
            67000,
            "1000123",
            "full",
            "/krstock/order/v1/modify",
            {
                "org_mkt_orr_no": "1000123",
                "all_pat_dit_cd": "1",
                "iem_cd": "005930",
                "cor_qty": 1,
                "cor_pr": 67000,
                "sop_cnd_pr": 0,
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
        ),
        (
            "modify",
            "buy",
            1,
            67000,
            "1000124",
            "partial",
            "/krstock/order/v1/modify",
            {
                "org_mkt_orr_no": "1000124",
                "all_pat_dit_cd": "2",
                "iem_cd": "005930",
                "cor_qty": 1,
                "cor_pr": 67000,
                "sop_cnd_pr": 0,
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
        ),
        (
            "cancel",
            "buy",
            None,
            None,
            "1000130",
            "full",
            "/krstock/order/v1/cancel",
            {
                "org_mkt_orr_no": "1000130",
                "all_pat_dit_cd": "1",
                "iem_cd": "005930",
            },
        ),
        (
            "cancel",
            "buy",
            2,
            None,
            "1000131",
            "partial",
            "/krstock/order/v1/cancel",
            {
                "org_mkt_orr_no": "1000131",
                "all_pat_dit_cd": "2",
                "iem_cd": "005930",
                "cor_qty": 2,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_order_body_is_independently_observed_for_all_six_routes(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    side: str,
    quantity: int | None,
    price: int | None,
    original: str | None,
    scope: str | None,
    expected_path: str,
    expected_input: dict[str, Any],
) -> None:
    suffix = "body-" + operation + "-" + side + "-" + (scope or "none")
    ref = await account(seeded_engine, suffix)
    intent = OrderIntent(
        operation, side, "005930", quantity, price, original, scope, ref
    )
    ledger = NHPlugMockLedger(seeded_engine)
    row, should_claim = await ledger.create_intent(
        intent,
        readiness=READY,
        idempotency_key="body_" + suffix + "_123456789",
        order_date=date.today(),
    )
    assert should_claim
    broker = FakeBroker(number=str(100000 + row["id"]))
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for(suffix)
    result = await client.dispatch_claimed_order(
        ledger,
        row["id"],
        row["client_request_id"],
        row["body_digest"],
        ref,
        keys={1: KEY},
        identity=IDENTITY,
        readiness=READY,
        timing=Stage2Timing(),
        dry_run=False,
        confirm=True,
    )
    assert result.state == "uncertain" and len(broker.requests) == 1
    assert broker.requests[0] == (
        expected_path,
        {"Input_0": {"act_no": "MOCK-" + suffix, **expected_input}},
    )


@pytest.mark.parametrize(
    "mutated",
    ["orr_qty", "orr_pr", "iem_cd", "nmn_pr_tp_cd", "sor_mkt_sli_yn", "act_no"],
)
@pytest.mark.asyncio
async def test_post_build_body_mutation_withdraws_before_send(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mutated: str,
) -> None:
    suffix = "mutation-" + mutated
    ledger, row, ref = await intent_row(seeded_engine, suffix)
    original_build = client_module.build_body

    def changed(intent: OrderIntent, act_no: str) -> tuple[str, dict[str, Any]]:
        path, body = original_build(intent, act_no)
        body[mutated] = "malicious"
        return path, body

    monkeypatch.setattr(client_module, "build_body", changed)
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for(suffix)
    with pytest.raises(LedgerConflict, match="order_body_differs_from_claim"):
        await client.dispatch_claimed_order(
            ledger,
            row["id"],
            row["client_request_id"],
            row["body_digest"],
            ref,
            keys={1: KEY},
            identity=IDENTITY,
            readiness=READY,
            timing=Stage2Timing(),
            dry_run=False,
            confirm=True,
        )
    assert (
        broker.requests == [] and (await ledger.get(row["id"]))["state"] == "withdrawn"
    )


@pytest.mark.parametrize(
    ("operation", "side", "quantity", "price", "original", "scope", "golden"),
    [
        (
            "place",
            "buy",
            1,
            67400,
            None,
            None,
            "a4793578393129f350e1c6c8ebf6b014f4f365a44b112a4153a2a9e8c4b2a416",
        ),
        (
            "place",
            "sell",
            3,
            71000,
            None,
            None,
            "4f2800121e82f2c497abc77f8fcab597e645f7f49d3e96b45bd498f01afb85fb",
        ),
        (
            "modify",
            "buy",
            1,
            67000,
            "1000123",
            "full",
            "0de1c5c727a6169a5818633547d6e0bbe4582a1c801c5183eeaf43ac892a819a",
        ),
        (
            "modify",
            "buy",
            1,
            67000,
            "1000124",
            "partial",
            "a0450b4ca9605972c1d6e8261aae8cee739ccc7e7969f0e5616174977cebf787",
        ),
        (
            "cancel",
            "buy",
            None,
            None,
            "1000130",
            "full",
            "ffecfc4affd110a0f81a3c1ac033d87e423898121e852b01dc485ea9c69b51be",
        ),
        (
            "cancel",
            "buy",
            2,
            None,
            "1000131",
            "partial",
            "972c72857e8b939a22691b3fc75ef73d87dea515009d3d7c10c3eee27617a382",
        ),
    ],
)
@pytest.mark.asyncio
async def test_six_golden_digests_match_generated_db_column(
    seeded_engine: AsyncEngine,
    operation: str,
    side: str,
    quantity: int | None,
    price: int | None,
    original: str | None,
    scope: str | None,
    golden: str,
) -> None:
    ref = UUID("3f2b8c1e-7a4d-4e6b-9c0a-5d1e2f3a4b5c")
    intent = OrderIntent(
        operation, side, "005930", quantity, price, original, scope, ref
    )
    assert body_digest(intent) == golden
    async with seeded_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_account_ref(account_ref) VALUES (:ref) ON CONFLICT DO NOTHING"
                ),
                {"ref": ref},
            )
            generated = (
                await conn.execute(
                    text(
                        "INSERT INTO review.nhplug_mock_order_ledger "
                        "(client_request_id,account_ref,idempotency_key,attempt_no,order_date,operation_kind,"
                        "side,symbol,quantity,price,original_order_id,amend_scope) VALUES "
                        "(:request,:ref,:key,1,:day,:op,:side,'005930',:qty,:price,:org,:scope) RETURNING body_digest"
                    ),
                    {
                        "request": uuid4(),
                        "ref": ref,
                        "key": "golden_" + uuid4().hex[:20],
                        "day": date.today(),
                        "op": operation,
                        "side": side,
                        "qty": quantity,
                        "price": price,
                        "org": original,
                        "scope": scope,
                    },
                )
            ).scalar_one()
            assert generated == golden
        finally:
            await transaction.rollback()


@pytest.mark.parametrize(
    ("operation", "quantity", "price", "original", "scope"),
    [
        ("place", None, 67400, None, None),
        ("place", 1, None, None, None),
        ("modify", 1, 67000, "123", None),
        ("modify", 1, 67000, None, "full"),
        ("cancel", None, None, "123", None),
        ("cancel", None, None, None, "full"),
        ("cancel", None, None, "123", "partial"),
    ],
)
@pytest.mark.asyncio
async def test_nullable_body_operands_cannot_pass_db_check(
    seeded_engine: AsyncEngine,
    operation: str,
    quantity: int | None,
    price: int | None,
    original: str | None,
    scope: str | None,
) -> None:
    ref = await account(seeded_engine, "nullable-" + uuid4().hex[:12])
    with pytest.raises(IntegrityError):
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_order_ledger "
                    "(client_request_id,account_ref,idempotency_key,attempt_no,order_date,operation_kind,"
                    "side,symbol,quantity,price,original_order_id,amend_scope) VALUES "
                    "(:request,:ref,:key,1,:day,:op,'buy','005930',:qty,:price,:org,:scope)"
                ),
                {
                    "request": uuid4(),
                    "ref": ref,
                    "key": "nullable_" + uuid4().hex[:20],
                    "day": date.today(),
                    "op": operation,
                    "qty": quantity,
                    "price": price,
                    "org": original,
                    "scope": scope,
                },
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("lease_machine_id", "fabricated"),
        ("success_rsp_cd", "00000"),
        ("reject_rsp_cd", "40310"),
        ("ack_evidence_order_id", "123"),
        ("filled_qty", 1),
        ("reconcile_state", "verified"),
        ("manual_review_reason", "fabricated"),
    ],
)
@pytest.mark.asyncio
async def test_insert_cannot_start_with_evidence_or_claim_fields(
    seeded_engine: AsyncEngine,
    column: str,
    value: object,
) -> None:
    ref = await account(seeded_engine, "dirty-" + uuid4().hex[:12])
    with pytest.raises(Exception, match="intent must be clean"):
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_order_ledger "
                    "(client_request_id,account_ref,idempotency_key,attempt_no,order_date,operation_kind,"
                    f"side,symbol,quantity,price,{column}) VALUES "
                    "(:request,:ref,:key,1,:day,'place','buy','005930',1,67400,:value)"
                ),
                {
                    "request": uuid4(),
                    "ref": ref,
                    "key": "dirty_" + uuid4().hex[:20],
                    "day": date.today(),
                    "value": value,
                },
            )


@pytest.mark.asyncio
async def test_sending_cannot_be_accepted_as_own_evidence_without_reconcile(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "forged-source")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    with pytest.raises(Exception, match="response acceptance proof absent"):
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='accepted', "
                    "broker_order_id='123', ack_order_id='123', ack_source='own_evidence', "
                    "lease_closed_at=now(), dispatcher_done_at=now() WHERE id=:id"
                ),
                {"id": row["id"]},
            )
    assert (await ledger.get(row["id"]))["state"] == "sending"


@pytest.mark.asyncio
async def test_filled_requires_independent_filled_scope_and_exact_quantities(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "fill-evidence")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", "223")
    )
    assert await ledger.verify_own_number(row["id"], listing(223), readiness=READY)
    all_row = OrderRow(
        223,
        "005930",
        1,
        1,
        0,
        side="buy",
        order_price=Decimal(67400),
        avg_fill_price=Decimal(67400),
    )
    all_orders = OrderListing("all", True, (all_row,), pages=1)
    open_orders = OrderListing("open", True, (), pages=1)
    assert (
        await ledger.reconcile_bound(
            row["id"], all_orders, open_orders, None, readiness=READY
        )
        == "unknown"
    )
    assert (await ledger.get(row["id"]))["state"] == "accepted"
    mismatched_fill = OrderListing(
        "filled", True, (OrderRow(223, "005930", 1, 0, 1),), pages=1
    )
    assert (
        await ledger.reconcile_bound(
            row["id"], all_orders, open_orders, mismatched_fill, readiness=READY
        )
        == "unknown"
    )
    filled_orders = OrderListing("filled", True, (all_row,), pages=1)
    assert (
        await ledger.reconcile_bound(
            row["id"], all_orders, open_orders, filled_orders, readiness=READY
        )
        == "filled"
    )
    final = await ledger.get(row["id"])
    assert final is not None and final["state"] == "filled" and final["filled_qty"] == 1
    assert (
        final["reconcile_state"] == "verified"
        and final["evidence"]["filled_order"]["order_no"] == "223"
    )


@pytest.mark.asyncio
async def test_cancelled_requires_own_cancel_ack(seeded_engine: AsyncEngine) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "cancel-evidence")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", "323")
    )
    assert await ledger.verify_own_number(row["id"], listing(323), readiness=READY)
    root = OrderRow(
        323, "005930", 1, 0, 0, side="buy", order_price=Decimal(67400), cancelled_qty=1
    )
    all_orders = OrderListing("all", True, (root,), pages=1)
    open_orders = OrderListing("open", True, (), pages=1)
    assert (
        await ledger.reconcile_bound(
            row["id"], all_orders, open_orders, None, readiness=READY
        )
        == "unknown"
    )
    cancel_intent = OrderIntent(
        "cancel", "buy", "005930", None, None, "323", "full", ref
    )
    cancel, should_claim = await ledger.create_intent(
        cancel_intent,
        readiness=READY,
        idempotency_key="cancel_evidence_123456",
        order_date=date.today(),
    )
    assert should_claim
    cancel_claim = await ledger.claim(
        cancel["id"], cancel["client_request_id"], cancel["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(cancel_claim)
    assert await ledger.record_final(
        cancel_claim, DispatchOutcome("uncertain", "no_proof_code", "324")
    )
    cancel_broker = OrderRow(
        324, "005930", 1, 0, 0, side="buy", original_order_no=323, cancelled_qty=1
    )
    assert await ledger.verify_own_number(
        cancel["id"],
        OrderListing("all", True, (cancel_broker,), pages=1),
        readiness=READY,
    )
    assert (
        await ledger.reconcile_bound(
            row["id"], all_orders, open_orders, None, readiness=READY
        )
        == "cancelled"
    )
    assert (await ledger.get(row["id"]))["state"] == "cancelled"


async def authorize_candidate(
    engine: AsyncEngine, row: dict[str, Any], number: str
) -> UUID:
    auth = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_operator_authorization "
                "(id,kind,target_row_id,account_ref,order_date,body_digest,candidate_order_id,evidence,operator_id,reason) "
                "VALUES (:id,'bind_candidate',:target,:acct,:day,:digest,:number,CAST(:evidence AS jsonb),'test-operator','manual match')"
            ),
            {
                "id": auth,
                "target": row["id"],
                "acct": row["account_ref"],
                "day": row["order_date"],
                "digest": row["body_digest"],
                "number": number,
                "evidence": json.dumps({"broker_number": number}),
            },
        )
    return auth


@pytest.mark.asyncio
async def test_candidate_only_then_operator_bind_after_dispatcher_done(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "candidate")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code")
    )
    sending_at = (await ledger.get(row["id"]))["sending_at"]
    broker_time = sending_at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%H%M%S%f")[:9]
    candidate = OrderRow(
        423,
        "005930",
        1,
        0,
        1,
        side="buy",
        order_price=Decimal(67400),
        order_time=broker_time,
    )
    candidates = await ledger.record_uncertain_candidates(
        row["id"],
        OrderListing("all", True, (candidate,), pages=1),
        readiness=READY,
    )
    assert candidates == ("423",)
    pending = await ledger.get(row["id"])
    assert (
        pending is not None
        and pending["state"] == "uncertain"
        and pending["broker_order_id"] is None
    )
    auth = await authorize_candidate(seeded_engine, pending, "423")
    assert await ledger.bind_operator_candidate(row["id"], "423", auth, readiness=READY)
    final = await ledger.get(row["id"])
    assert (
        final is not None
        and final["state"] == "accepted"
        and final["ack_source"] == "operator"
    )
    assert not await ledger.bind_operator_candidate(
        row["id"], "423", auth, readiness=READY
    )
    async with seeded_engine.connect() as conn:
        consumed = (
            await conn.execute(
                text(
                    "SELECT consumed_by_row_id FROM review.nhplug_mock_operator_authorization WHERE id=:id"
                ),
                {"id": auth},
            )
        ).scalar_one()
    assert consumed == row["id"]


@pytest.mark.asyncio
async def test_live_dispatcher_blocks_candidate_and_own_ack_wins(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "candidate-race")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim, lease_seconds=1)
    await asyncio.sleep(1.1)
    assert (await ledger.recover_expired())[2] >= 1
    recovered = await ledger.get(row["id"])
    assert (
        recovered is not None
        and recovered["state"] == "uncertain"
        and recovered["dispatcher_done_at"] is None
    )
    broker_time = (
        recovered["sending_at"]
        .astimezone(ZoneInfo("Asia/Seoul"))
        .strftime("%H%M%S%f")[:9]
    )
    candidate = OrderRow(
        523,
        "005930",
        1,
        0,
        1,
        side="buy",
        order_price=Decimal(67400),
        order_time=broker_time,
    )
    assert await ledger.record_uncertain_candidates(
        row["id"],
        OrderListing("all", True, (candidate,), pages=1),
        readiness=READY,
    ) == ("523",)
    auth = await authorize_candidate(seeded_engine, recovered, "523")
    with pytest.raises(Exception, match="dispatcher may still write"):
        await ledger.bind_operator_candidate(row["id"], "523", auth, readiness=READY)
    assert (await ledger.get(row["id"]))["state"] == "uncertain"
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", "524")
    )
    after_late = await ledger.get(row["id"])
    assert after_late is not None and after_late["ack_evidence_order_id"] == "524"
    assert after_late["dispatcher_done_at"] is not None
    with pytest.raises(Exception, match="candidate bind mismatch"):
        await ledger.bind_operator_candidate(row["id"], "523", auth, readiness=READY)
    assert await ledger.verify_own_number(row["id"], listing(524), readiness=READY)
    assert (await ledger.get(row["id"]))["broker_order_id"] == "524"


@pytest.mark.asyncio
async def test_abandon_needs_host_death_listing_and_operator_authorization(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "abandon")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim, lease_seconds=1)
    await asyncio.sleep(1.1)
    assert (await ledger.recover_expired())[2] >= 1
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", "123")
    )
    pending = await ledger.get(row["id"])
    assert pending is not None and pending["state"] == "uncertain"
    empty = OrderListing("all", True, (), pages=1)
    monkeypatch.setattr(
        lease_host, "process_gone_on_lease_host", lambda identity: False
    )
    with pytest.raises(LedgerConflict, match="lease_process_not_proven_gone"):
        await ledger.abandon_with_authorization(
            row["id"], uuid4(), empty, readiness=READY
        )
    monkeypatch.setattr(lease_host, "process_gone_on_lease_host", lambda identity: True)
    with pytest.raises(LedgerConflict, match="own_number_present"):
        await ledger.abandon_with_authorization(
            row["id"], uuid4(), listing(123), readiness=READY
        )
    auth = uuid4()
    async with seeded_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_operator_authorization "
                "(id,kind,target_row_id,account_ref,order_date,body_digest,evidence,grace_until,operator_id,reason) "
                "VALUES (:id,'abandon',:target,:acct,:day,:digest,CAST(:evidence AS jsonb),:grace,'test-operator',"
                "'accept unresolved duplicate risk')"
            ),
            {
                "id": auth,
                "target": row["id"],
                "acct": ref,
                "day": row["order_date"],
                "digest": row["body_digest"],
                "evidence": json.dumps(
                    {
                        "process_gone": True,
                        "listing_complete": True,
                        "grace_elapsed": True,
                    }
                ),
                "grace": pending["lease_expires_at"] + timedelta(milliseconds=100),
            },
        )
    with pytest.raises(LedgerConflict, match="own_number_present"):
        await ledger.abandon_with_authorization(
            row["id"], auth, listing(123), readiness=READY
        )
    assert await ledger.abandon_with_authorization(
        row["id"], auth, empty, readiness=READY
    )
    final = await ledger.get(row["id"])
    assert final is not None and final["state"] == "abandoned"


async def start_claim_process(
    engine: AsyncEngine, row: dict[str, Any], mode: str
) -> asyncio.subprocess.Process:
    payload = {
        "row_id": row["id"],
        "request_id": str(row["client_request_id"]),
        "digest": row["body_digest"],
        "account_ref": str(row["account_ref"]),
        "machine": "worker-machine",
        "boot": "worker-boot",
        "namespace": "worker-ns",
        "mode": mode,
    }
    environment = os.environ.copy()
    environment["NHPLUG_TEST_WORKER_DB_URL"] = str(engine.url)
    environment["NHPLUG_TEST_WORKER_PAYLOAD"] = json.dumps(payload)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("claim_worker.py")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    assert process.stdout is not None
    assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"ready"
    return process


async def finish_claim_process(process: asyncio.subprocess.Process) -> str:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"go\n")
    await process.stdin.drain()
    status = (await asyncio.wait_for(process.stdout.readline(), 10)).decode().strip()
    assert await asyncio.wait_for(process.wait(), 10) == 0
    return status


@pytest.mark.asyncio
async def test_two_independent_processes_race_for_one_claim(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, _ = await intent_row(seeded_engine, "process-race")
    first = await start_claim_process(seeded_engine, row, "claim")
    second = await start_claim_process(seeded_engine, row, "claim")
    outcomes = await asyncio.gather(
        finish_claim_process(first), finish_claim_process(second)
    )
    assert sorted(outcomes) == ["claimed", "rejected"]
    stored = await ledger.get(row["id"])
    assert (
        stored is not None
        and stored["state"] == "claimed"
        and stored["sending_at"] is None
    )


@pytest.mark.asyncio
async def test_process_death_after_fence_blocks_replay_until_recovery(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "process-fence")
    dead = await start_claim_process(seeded_engine, row, "fence_die")
    assert await finish_claim_process(dead) == "fenced"
    stored = await ledger.get(row["id"])
    assert (
        stored is not None
        and stored["state"] == "sending"
        and stored["sending_at"] is not None
    )
    replay = await start_claim_process(seeded_engine, row, "claim")
    assert await finish_claim_process(replay) == "rejected"
    await asyncio.sleep(1.1)
    assert (await ledger.recover_expired())[2] >= 1
    recovered = await ledger.get(row["id"])
    assert recovered is not None and recovered["state"] == "uncertain"
    with pytest.raises(LedgerConflict, match="in_flight_order_exists"):
        await ledger.create_intent(
            OrderIntent("place", "buy", "005930", 1, 67500, None, None, ref),
            readiness=READY,
            idempotency_key="process_new_123456789",
            order_date=date.today() + timedelta(days=1),
        )
