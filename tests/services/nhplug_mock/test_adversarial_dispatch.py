# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""Independent fake-broker and process failure tests for mock dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.brokers.nhplug.client as client_module
from app.services.brokers.nhplug.order_evidence import OrderListing, OrderRow
from app.services.nhplug_mock.account_identity import (
    AccountIdentityError,
    KeyMaterial,
)
from app.services.nhplug_mock.intent import OrderIntent
from app.services.nhplug_mock.ledger import (
    DispatchOutcome,
    LedgerConflict,
    NHPlugMockLedger,
)
from app.services.nhplug_mock.transport import Stage2Timing
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.nhplug_mock.test_dispatch_state_machine import (  # noqa: F401
    IDENTITY,
    KEY,
    READY,
    FakeBroker,
    account,
    client_for,
    intent_row,
    listing,
    mock_gate,
    nhplug_engine,
    scoped,
    seeded_engine,
)

pytestmark = pytest.mark.integration
RESERVING = {"intent", "claimed", "sending", "uncertain"}


async def _dispatch(
    client: Any, ledger: Any, row: dict[str, Any], ref: UUID, **kw: Any
):
    return await client.dispatch_claimed_order(
        ledger,
        row["id"],
        row["client_request_id"],
        row["body_digest"],
        ref,
        keys={1: KEY},
        readiness=READY,
        timing=kw.pop("timing", Stage2Timing()),
        dry_run=False,
        confirm=True,
    )


async def _to_uncertain(seeded: AsyncEngine, suffix: str, number: str | None = None):
    ledger, row, ref = await intent_row(seeded, suffix)
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", number)
    )
    return ledger, row, ref, claim


# ---------------------------------------------------------------- item 1
async def _spawn(engine: AsyncEngine, payload: dict[str, Any]):
    environment = os.environ.copy()
    validate_run_owned_database_url(engine.url)
    environment["NHPLUG_TEST_WORKER_DB_URL"] = engine.url.render_as_string(
        hide_password=False
    )
    environment["NHPLUG_PROBE_PAYLOAD"] = json.dumps(payload)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.services.nhplug_mock.dispatch_test_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=str(Path(__file__).resolve().parents[3]),
    )
    line = await asyncio.wait_for(process.stdout.readline(), 30)
    if not line.strip().startswith(b"ready"):
        err = await process.stderr.read()
        raise AssertionError(f"worker not ready: {line!r} {err[-2000:]!r}")
    return process


async def _finish(process: Any) -> str:
    process.stdin.write(b"go\n")
    await process.stdin.drain()
    status = (await asyncio.wait_for(process.stdout.readline(), 30)).decode().strip()
    code = await asyncio.wait_for(process.wait(), 30)
    if code != 0:
        err = await process.stderr.read()
        raise AssertionError(f"worker rc={code} {err[-2000:]!r}")
    return status


@pytest.mark.asyncio
async def test_probe_cross_process_dispatch_race_one_wire_request(
    seeded_engine: AsyncEngine, tmp_path: Path
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "xproc-race")
    wire_log = tmp_path / "wire.jsonl"
    wire_log.write_text("")
    base = {
        "row_id": row["id"],
        "request_id": str(row["client_request_id"]),
        "digest": row["body_digest"],
        "account_ref": str(ref),
        "act_no": "MOCK-xproc-race",
        "key_id": KEY.key_id,
        "key": KEY.key.decode("latin-1"),
        "wire_log": str(wire_log),
    }
    variants = [
        base,
        base,
        base,
        {**base, "request_id": str(uuid4())},
        {**base, "digest": "0" * 64},
    ]
    processes = [await _spawn(seeded_engine, payload) for payload in variants]
    outcomes = await asyncio.gather(*(_finish(p) for p in processes))
    wire = [json.loads(line) for line in wire_log.read_text().splitlines() if line]
    assert len(wire) == 1, (outcomes, wire)
    assert sorted(outcomes) == sorted(
        ["outcome:uncertain"] + ["conflict:claim_rejected"] * 4
    ), outcomes
    stored = await ledger.get(row["id"])
    assert stored["state"] == "uncertain" and stored["ack_evidence_order_id"] == "901"
    # A different verified account cannot claim this row either.
    other = await client_for("xproc-other")
    broker = FakeBroker()
    original = client_module.GatedTransport
    client_module.GatedTransport = broker.transport
    try:
        with pytest.raises(LedgerConflict, match="account_ref_mismatch"):
            await _dispatch(other, ledger, row, ref)
    finally:
        client_module.GatedTransport = original
    assert broker.requests == []


@pytest.mark.asyncio
async def test_probe_fence_commit_ambiguity_never_sends(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "fence-ambig")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    original_fence = ledger.fence

    async def committed_then_lost(claim: Any, **kw: Any) -> bool:
        assert await original_fence(claim, **{**kw, "lease_seconds": 1})
        raise ConnectionResetError("commit acknowledgement lost")

    monkeypatch.setattr(ledger, "fence", committed_then_lost)
    client = await client_for("fence-ambig")
    with pytest.raises(ConnectionResetError):
        await _dispatch(client, ledger, row, ref)
    assert broker.requests == []
    assert (await ledger.get(row["id"]))["state"] == "sending"
    monkeypatch.setattr(ledger, "fence", original_fence)
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await _dispatch(client, ledger, row, ref)
    assert broker.requests == []
    await asyncio.sleep(1.1)
    await ledger.recover_expired()
    assert (await ledger.get(row["id"]))["state"] == "uncertain"


@pytest.mark.asyncio
async def test_probe_fence_rejected_after_claim_deadline_never_sends(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "fence-late")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("fence-late")
    original_codes = ledger.proof_codes

    async def slow_codes(path: str):
        await asyncio.sleep(1.3)
        return await original_codes(path)

    monkeypatch.setattr(ledger, "proof_codes", slow_codes)
    raised = None
    try:
        await _dispatch(
            client, ledger, row, ref, timing=Stage2Timing(claim_window_seconds=1)
        )
    except LedgerConflict as exc:
        raised = exc.code
    stored = await ledger.get(row["id"])
    assert broker.requests == [], (
        broker.requests,
        stored["state"],
        stored["sending_at"],
    )
    assert raised == "fence_rejected" and stored["state"] == "claimed"


@pytest.mark.asyncio
async def test_probe_result_write_failure_via_dispatch_keeps_sending_no_resend(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "record-fail")
    broker = FakeBroker(number="611")
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)

    async def broken_record(claim: Any, outcome: DispatchOutcome) -> bool:
        raise OSError("result commit failed")

    original_record = ledger.record_final
    monkeypatch.setattr(ledger, "record_final", broken_record)
    client = await client_for("record-fail")
    with pytest.raises(OSError, match="result commit failed"):
        await _dispatch(client, ledger, row, ref, timing=Stage2Timing(lease_seconds=3))
    assert len(broker.requests) == 1
    assert (await ledger.get(row["id"]))["state"] == "sending"
    monkeypatch.setattr(ledger, "record_final", original_record)
    resumed, should_claim = await ledger.create_intent(
        OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref),
        readiness=READY,
        idempotency_key=row["idempotency_key"],
        order_date=row["order_date"],
    )
    assert resumed["id"] == row["id"] and should_claim is False
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await _dispatch(client, ledger, row, ref)
    assert len(broker.requests) == 1
    await asyncio.sleep(3.1)
    await ledger.recover_expired()
    after = await ledger.get(row["id"])
    assert after["state"] == "uncertain" and after["ack_evidence_order_id"] is None
    with pytest.raises(LedgerConflict, match="in_flight_order_exists"):
        await ledger.create_intent(
            OrderIntent("place", "buy", "005930", 1, 67500, None, None, ref),
            readiness=READY,
            idempotency_key="record_fail_new_key_123",
            order_date=row["order_date"] + timedelta(days=1),
        )


@pytest.mark.asyncio
async def test_probe_broker_accepts_then_connection_drops(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "drop-after-accept")
    seen: list[str] = []

    class DroppingWire(httpx.AsyncBaseTransport):
        def arm(self, deadline: float) -> None:
            return None

        async def hard_close(self, timeout: float) -> None:
            return None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            raise httpx.ReadError("peer reset after broker accepted", request=request)

    monkeypatch.setattr(client_module, "GatedTransport", DroppingWire)
    client = await client_for("drop-after-accept")
    outcome = await _dispatch(client, ledger, row, ref)
    assert outcome.state == "uncertain" and outcome.reason == "ReadError"
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await _dispatch(client, ledger, row, ref)
    assert seen == ["/krstock/order/v1/cashBuy"]


@pytest.mark.asyncio
async def test_probe_key_mismatch_in_dispatch_zero_writes(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "dispatch-keymis")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("dispatch-keymis")

    async def counts() -> tuple[int, int]:
        async with seeded_engine.connect() as conn:
            refs = (
                await conn.execute(
                    text("SELECT count(*) FROM review.nhplug_mock_account_ref")
                )
            ).scalar_one()
            binds = (
                await conn.execute(
                    text("SELECT count(*) FROM review.nhplug_mock_account_binding")
                )
            ).scalar_one()
        return refs, binds

    before = await counts()
    with pytest.raises(AccountIdentityError, match="key_mismatch"):
        await client.dispatch_claimed_order(
            ledger,
            row["id"],
            row["client_request_id"],
            row["body_digest"],
            ref,
            keys={1: KeyMaterial(1, KEY.key_id, b"a-different-physical-key")},
            readiness=READY,
            timing=Stage2Timing(),
            dry_run=False,
            confirm=True,
        )
    assert await counts() == before
    stored = await ledger.get(row["id"])
    assert stored["state"] == "intent" and stored["claim_token"] is None
    assert broker.requests == []


# ---------------------------------------------------------------- item 3
@pytest.mark.asyncio
async def test_probe_forged_anomaly_without_own_number_is_refused(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref, _ = await _to_uncertain(seeded_engine, "forged-anomaly")
    forged = {
        "listing_order_id": None,
        "listing_complete": True,
        "listing_scope": "all",
        "account_ref": str(row["account_ref"]),
        "order_date": str(row["order_date"]),
        "attributes_match": False,
    }
    with pytest.raises(
        Exception, match="uncertain anomaly positive listing proof absent"
    ):
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='anomaly', "
                    "requires_manual_review=true, manual_review_reason='forged', "
                    "evidence=CAST(:e AS jsonb), last_reconcile=CAST(:e AS jsonb) WHERE id=:id"
                ),
                {"id": row["id"], "e": json.dumps(forged)},
            )
    assert (await ledger.get(row["id"]))["state"] == "uncertain"


def _payloads(row: dict[str, Any]) -> dict[str, str]:
    e = json.dumps(
        {
            "listing_order_id": "123",
            "listing_complete": True,
            "listing_scope": "all",
            "account_ref": str(row["account_ref"]),
            "order_date": str(row["order_date"]),
            "attributes_match": True,
        }
    )
    common = "lease_closed_at=coalesce(lease_closed_at,now()), dispatcher_done_at=coalesce(dispatcher_done_at,now()), sending_at=coalesce(sending_at,now()), lease_expires_at=coalesce(lease_expires_at,now()), claim_token=coalesce(claim_token,gen_random_uuid()), claimed_at=coalesce(claimed_at,now())"
    ids = "broker_order_id='123', ack_order_id='123'"
    qty = f"reconcile_state='verified', evidence='{e}'::jsonb, last_reconcile='{e}'::jsonb"
    return {
        "withdrawn": "state='withdrawn', withdraw_reason='x'",
        "rejected": f"state='rejected', reject_rsp_cd='40310', {common}",
        "accepted_response": f"state='accepted', {ids}, ack_source='response', success_rsp_cd='00000', {common}",
        "accepted_own": f"state='accepted', {ids}, ack_source='own_evidence', {qty}, {common}",
        "accepted_operator": f"state='accepted', {ids}, ack_source='operator', resolution_authorization_id=gen_random_uuid(), {common}",
        "open": f"state='open', {ids}, ack_source='own_evidence', filled_qty=0, open_qty=1, cancelled_qty=0, modified_qty=0, {qty}, {common}",
        "filled": f"state='filled', {ids}, ack_source='own_evidence', filled_qty=1, open_qty=0, cancelled_qty=0, modified_qty=0, {qty}, {common}",
        "cancelled": f"state='cancelled', {ids}, ack_source='own_evidence', filled_qty=0, open_qty=0, cancelled_qty=1, modified_qty=0, {qty}, {common}",
        "anomaly": f"state='anomaly', requires_manual_review=true, manual_review_reason='x', {qty}, {common}",
        "abandoned": f"state='abandoned', resolution_authorization_id=gen_random_uuid(), {common}",
        "intent": "state='intent', claim_token=NULL, claimed_at=NULL",
    }


@pytest.mark.asyncio
async def test_probe_no_direct_sql_release_from_reserving_states(
    seeded_engine: AsyncEngine,
) -> None:
    """From each legally reached reserving state, every direct UPDATE that would
    leave the reservation set is refused (no auth rows exist for these rows)."""

    attempts = 0
    for start in ("intent", "claimed", "sending", "uncertain"):
        ledger, row, ref = await intent_row(seeded_engine, "pairs-" + start)
        if start != "intent":
            claim = await ledger.claim(
                row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
            )
        if start in {"sending", "uncertain"}:
            assert await ledger.fence(claim)
        if start == "uncertain":
            assert await ledger.record_final(
                claim, DispatchOutcome("uncertain", "no_proof_code")
            )
        for name, assignment in _payloads(row).items():
            if start in {"intent", "claimed"} and name == "withdrawn":
                continue  # legal pre-fence withdrawal (T2/T4/T10/T10i)
            async with seeded_engine.connect() as conn:
                trans = await conn.begin()
                try:
                    try:
                        async with conn.begin_nested():
                            await conn.execute(
                                text(
                                    f"UPDATE review.nhplug_mock_order_ledger SET {assignment} WHERE id=:id"
                                ),
                                {"id": row["id"]},
                            )
                    except Exception:
                        pass
                    state = (
                        await conn.execute(
                            text(
                                "SELECT state FROM review.nhplug_mock_order_ledger WHERE id=:id"
                            ),
                            {"id": row["id"]},
                        )
                    ).scalar_one()
                    attempts += 1
                    assert state in RESERVING, (start, name, state)
                finally:
                    await trans.rollback()
    assert attempts == 42


# ---------------------------------------------------------------- item 2
@pytest.mark.asyncio
async def test_probe_partial_modify_requires_exact_reflected_quantity(
    seeded_engine: AsyncEngine,
) -> None:
    ref = await account(seeded_engine, "pm-drift")
    ledger = NHPlugMockLedger(seeded_engine)
    place, _ = await ledger.create_intent(
        OrderIntent("place", "buy", "005930", 5, 67400, None, None, ref),
        readiness=READY,
        idempotency_key="pm_drift_place_123456",
        order_date=date.today(),
    )
    c = await ledger.claim(
        place["id"], place["client_request_id"], place["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(c)
    assert await ledger.record_final(
        c, DispatchOutcome("uncertain", "no_proof_code", "830")
    )
    root = OrderRow(830, "005930", 5, 0, 5, side="buy", order_price=Decimal(67400))
    assert await ledger.verify_own_number(
        place["id"],
        scoped(OrderListing("all", True, (root,), pages=1), place),
        readiness=READY,
    )
    modify, _ = await ledger.create_intent(
        OrderIntent("modify", "buy", "005930", 1, 67000, "830", "partial", ref),
        readiness=READY,
        idempotency_key="pm_drift_modify_123456",
        order_date=date.today(),
    )
    c = await ledger.claim(
        modify["id"], modify["client_request_id"], modify["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(c)
    assert await ledger.record_final(
        c, DispatchOutcome("uncertain", "no_proof_code", "831")
    )
    successor = OrderRow(
        831,
        "005930",
        1,
        0,
        1,
        side="buy",
        original_order_no=830,
        order_price=Decimal(67000),
    )
    # Original reflects 2 modified shares, not the 1 this request applied.
    drifted_root = replace(root, open_qty=3, modified_qty=2)
    all_orders = scoped(
        OrderListing("all", True, (drifted_root, successor), pages=1), modify
    )
    open_orders = scoped(
        OrderListing("open", True, (drifted_root, successor), pages=1), modify
    )
    assert await ledger.verify_own_number(modify["id"], all_orders, readiness=READY)
    assert (
        await ledger.reconcile_bound(
            modify["id"], all_orders, open_orders, None, readiness=READY
        )
        == "unknown"
    )
    assert (await ledger.get(modify["id"]))["state"] == "accepted"


@pytest.mark.asyncio
async def test_probe_abandon_db_guard_rejects_unproven_process_evidence(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "abandon-db")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim, lease_seconds=1)
    await asyncio.sleep(1.1)
    await ledger.recover_expired()
    pending = await ledger.get(row["id"])
    for missing in (
        "process_gone",
        "listing_complete",
        "grace_elapsed",
        "future_grace",
    ):
        evidence = {
            "process_gone": True,
            "listing_complete": True,
            "grace_elapsed": True,
        }
        if missing != "future_grace":
            evidence[missing] = False
        grace = (
            pending["lease_expires_at"] + timedelta(hours=6)
            if missing == "future_grace"
            else pending["lease_expires_at"] + timedelta(milliseconds=100)
        )
        auth = uuid4()
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_operator_authorization "
                    "(id,kind,target_row_id,account_ref,order_date,body_digest,evidence,grace_until,operator_id,reason) "
                    "VALUES (:id,'abandon',:t,:a,:d,:g,CAST(:e AS jsonb),:gr,'probe','probe')"
                ),
                {
                    "id": auth,
                    "t": row["id"],
                    "a": ref,
                    "d": row["order_date"],
                    "g": row["body_digest"],
                    "e": json.dumps(evidence),
                    "gr": grace,
                },
            )
        with pytest.raises(Exception, match="abandon positive process proof absent"):
            async with seeded_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE review.nhplug_mock_order_ledger SET state='abandoned', "
                        "resolution_authorization_id=:auth WHERE id=:id"
                    ),
                    {"id": row["id"], "auth": auth},
                )
        assert (await ledger.get(row["id"]))["state"] == "uncertain"


@pytest.mark.asyncio
async def test_probe_own_number_attribute_mismatch_goes_anomaly_not_accepted(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref, _ = await _to_uncertain(seeded_engine, "own-mismatch", "951")
    wrong = scoped(listing(951, price=99900), row)
    assert not await ledger.verify_own_number(row["id"], wrong, readiness=READY)
    stored = await ledger.get(row["id"])
    assert stored["state"] == "anomaly" and stored["broker_order_id"] is None


@pytest.mark.asyncio
async def test_probe_forged_dispatcher_done_enables_bind_without_gone_proof(
    seeded_engine: AsyncEngine,
) -> None:
    """DB cannot tell D's completion from any writer's: documents the limit."""

    ledger, row, ref = await intent_row(seeded_engine, "forge-done")
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim, lease_seconds=1)
    await asyncio.sleep(1.1)
    await ledger.recover_expired()
    async with seeded_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE review.nhplug_mock_order_ledger SET dispatcher_done_at=now() WHERE id=:id"
            ),
            {"id": row["id"]},
        )
    stored = await ledger.get(row["id"])
    assert stored["dispatcher_done_at"] is not None


@pytest.mark.asyncio
async def test_probe_arm_failure_is_pre_fence_withdrawal(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r5-P1 / T-UNC-6: arm fails before the fence -> T4 withdrawn, zero sends."""

    ledger, row, ref = await intent_row(seeded_engine, "arm-fail")
    sent: list[str] = []

    class BadArmWire(httpx.AsyncBaseTransport):
        def arm(self, deadline: float) -> None:
            raise ValueError("injected arm failure")

        async def hard_close(self, timeout: float) -> None:
            return None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            sent.append(request.url.path)
            return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(client_module, "GatedTransport", BadArmWire)
    client = await client_for("arm-fail")
    with pytest.raises(ValueError, match="injected arm failure"):
        await _dispatch(client, ledger, row, ref)
    stored = await ledger.get(row["id"])
    assert sent == []
    assert stored["state"] == "withdrawn" and stored["sending_at"] is None


@pytest.mark.asyncio
async def test_probe_second_dispatcher_write_after_done_is_zero_rows(
    seeded_engine: AsyncEngine,
) -> None:
    """T-EVID-2: after T8 (done), later D writes of any kind are 0 rows."""

    ledger, row, ref, claim = await _to_uncertain(seeded_engine, "evid2", "961")
    before = await ledger.get(row["id"])
    for outcome in (
        DispatchOutcome("uncertain", "late", "961"),
        DispatchOutcome("uncertain", "late", "962"),
        DispatchOutcome("uncertain", "late", None),
    ):
        assert await ledger.record_final(claim, outcome) is False
    after = await ledger.get(row["id"])
    assert after == before


@pytest.mark.asyncio
async def test_probe_partial_modify_under_applied_quantity_stays_unknown(
    seeded_engine: AsyncEngine,
) -> None:
    """Exact applied quantity: request 2, original reflects only 1 -> no T12."""

    ref = await account(seeded_engine, "pm-under")
    ledger = NHPlugMockLedger(seeded_engine)
    place, _ = await ledger.create_intent(
        OrderIntent("place", "buy", "005930", 5, 67400, None, None, ref),
        readiness=READY,
        idempotency_key="pm_under_place_123456",
        order_date=date.today(),
    )
    c = await ledger.claim(
        place["id"], place["client_request_id"], place["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(c)
    assert await ledger.record_final(
        c, DispatchOutcome("uncertain", "no_proof_code", "850")
    )
    root = OrderRow(850, "005930", 5, 0, 5, side="buy", order_price=Decimal(67400))
    assert await ledger.verify_own_number(
        place["id"],
        scoped(OrderListing("all", True, (root,), pages=1), place),
        readiness=READY,
    )
    modify, _ = await ledger.create_intent(
        OrderIntent("modify", "buy", "005930", 2, 67000, "850", "partial", ref),
        readiness=READY,
        idempotency_key="pm_under_modify_123456",
        order_date=date.today(),
    )
    c = await ledger.claim(
        modify["id"], modify["client_request_id"], modify["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(c)
    assert await ledger.record_final(
        c, DispatchOutcome("uncertain", "no_proof_code", "851")
    )
    successor = OrderRow(
        851,
        "005930",
        2,
        0,
        2,
        side="buy",
        original_order_no=850,
        order_price=Decimal(67000),
    )
    under = replace(root, open_qty=4, modified_qty=1)
    all_orders = scoped(OrderListing("all", True, (under, successor), pages=1), modify)
    open_orders = scoped(
        OrderListing("open", True, (under, successor), pages=1), modify
    )
    assert await ledger.verify_own_number(modify["id"], all_orders, readiness=READY)
    result = await ledger.reconcile_bound(
        modify["id"], all_orders, open_orders, None, readiness=READY
    )
    assert result == "unknown", result
    assert (await ledger.get(modify["id"]))["state"] == "accepted"
