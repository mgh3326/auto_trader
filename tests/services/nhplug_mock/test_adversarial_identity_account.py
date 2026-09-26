# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""Separate-process lease identity and verified account binding tests."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import signal
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.brokers.nhplug.client as client_module
from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockConfigurationError,
    NHPlugMockResponseError,
)
from app.services.brokers.nhplug.order_evidence import OrderListing
from app.services.nhplug_mock.account_identity import resolve_account_ref
from app.services.nhplug_mock.intent import OrderIntent
from app.services.nhplug_mock.lease_host import (
    _starttime,
    current_lease_identity,
    lease_identity_from_row,
    process_gone_on_lease_host,
)
from app.services.nhplug_mock.ledger import (
    LeaseIdentity,
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
    client_for,
    intent_row,
    mock_gate,
    nhplug_engine,
    scoped,
    seeded_engine,
)

pytestmark = pytest.mark.integration


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


def _client_with_accounts(rows: list[dict[str, str]]) -> NHPlugMockClient:
    async def token() -> str:
        return "probe-token"

    wire = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"rsp_cd": "00000", "Output_0": rows}, request=request
        )
    )
    return NHPlugMockClient(
        app_key="probe", app_secret="probe", token_provider=token, transport=wire
    )


# ------------------------------------------------------------------ B1
async def _spawn(engine: AsyncEngine, payload: dict[str, Any]):
    env = os.environ.copy()
    validate_run_owned_database_url(engine.url)
    env["NHPLUG_TEST_WORKER_DB_URL"] = engine.url.render_as_string(hide_password=False)
    env["NHPLUG_PROBE_PAYLOAD"] = json.dumps(payload)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.services.nhplug_mock.dispatch_test_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[3]),
    )
    line = (await asyncio.wait_for(proc.stdout.readline(), 30)).decode().strip()
    if not line.startswith("ready "):
        raise AssertionError(line + (await proc.stderr.read()).decode()[-2000:])
    return proc, int(line.split()[1])


async def _abandon_auth(
    engine: AsyncEngine, row: dict[str, Any], pending: dict[str, Any]
) -> UUID:
    auth = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_operator_authorization "
                "(id,kind,target_row_id,account_ref,order_date,body_digest,evidence,grace_until,operator_id,reason) "
                "VALUES (:id,'abandon',:t,:a,:d,:g,CAST(:e AS jsonb),:gr,'r2-probe','accept unresolved risk')"
            ),
            {
                "id": auth,
                "t": row["id"],
                "a": row["account_ref"],
                "d": row["order_date"],
                "g": row["body_digest"],
                "e": json.dumps(
                    {
                        "process_gone": True,
                        "listing_complete": True,
                        "grace_elapsed": True,
                    }
                ),
                "gr": pending["lease_expires_at"] + timedelta(milliseconds=200),
            },
        )
    return auth


@pytest.mark.asyncio
async def test_r2_b1_live_separate_sender_blocks_t14_until_it_really_exits(
    seeded_engine: AsyncEngine, tmp_path: Path
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "r2-b1-live")
    wire_log = tmp_path / "wire.jsonl"
    wire_log.write_text("")
    proc, child_pid = await _spawn(
        seeded_engine,
        {
            "row_id": row["id"],
            "request_id": str(row["client_request_id"]),
            "digest": row["body_digest"],
            "account_ref": str(ref),
            "act_no": "MOCK-r2-b1-live",
            "key_id": KEY.key_id,
            "key": KEY.key.decode("latin-1"),
            "wire_log": str(wire_log),
            "lease_seconds": 3,
            "stay_alive": True,
        },
    )
    proc.stdin.write(b"go\n")
    await proc.stdin.drain()
    status = (await asyncio.wait_for(proc.stdout.readline(), 30)).decode().strip()
    assert status == "outcome:uncertain", status
    stored = await ledger.get(row["id"])
    me = current_lease_identity()
    child_start = _starttime(Path(f"/proc/{child_pid}/stat").read_text())
    # Row identity is the separate sending process, not the test process or a caller value.
    assert (stored["lease_pid"], stored["lease_process_start"]) == (
        child_pid,
        child_start,
    )
    assert (
        stored["lease_machine_id"],
        stored["lease_boot_id"],
        stored["lease_pid_ns"],
    ) == (
        me.machine_id,
        me.boot_id,
        me.pid_ns,
    )
    identity = lease_identity_from_row(stored)
    assert not process_gone_on_lease_host(identity)
    await asyncio.sleep(3.3)
    pending = await ledger.get(row["id"])
    auth = await _abandon_auth(seeded_engine, row, pending)
    empty = scoped(OrderListing("all", True, (), pages=1), row)
    await asyncio.sleep(0.3)
    with pytest.raises(LedgerConflict, match="lease_process_not_proven_gone"):
        await ledger.abandon_with_authorization(row["id"], auth, empty, readiness=READY)
    os.kill(child_pid, signal.SIGSTOP)
    try:
        await asyncio.sleep(0.2)
        state = Path(f"/proc/{child_pid}/stat").read_text().split(")")[1].split()[0]
        assert state == "T"
        assert not process_gone_on_lease_host(identity)
        with pytest.raises(LedgerConflict, match="lease_process_not_proven_gone"):
            await ledger.abandon_with_authorization(
                row["id"], auth, empty, readiness=READY
            )
    finally:
        os.kill(child_pid, signal.SIGCONT)
    assert (await ledger.get(row["id"]))["state"] == "uncertain"
    proc.stdin.write(b"exit\n")
    await proc.stdin.drain()
    assert await asyncio.wait_for(proc.wait(), 30) == 0
    assert process_gone_on_lease_host(identity)
    assert await ledger.abandon_with_authorization(
        row["id"], auth, empty, readiness=READY
    )
    final = await ledger.get(row["id"])
    assert final["state"] == "abandoned"
    wire = [json.loads(line) for line in wire_log.read_text().splitlines() if line]
    assert [(w["pid"], w["path"]) for w in wire] == [
        (child_pid, "/krstock/order/v1/cashBuy")
    ]


@pytest.mark.asyncio
async def test_r2_b1_in_process_row_identity_is_this_sender(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "r2-b1-self")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("r2-b1-self")
    await _dispatch(client, ledger, row, ref)
    stored = await ledger.get(row["id"])
    me = current_lease_identity()
    assert lease_identity_from_row(stored) == me and me.pid == os.getpid()
    assert not process_gone_on_lease_host(lease_identity_from_row(stored))
    assert len(broker.requests) == 1


@pytest.mark.asyncio
async def test_r2_b1_caller_cannot_inject_identity_and_foreign_claim_blocks_send(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "r2-b1-foreign")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("r2-b1-foreign")
    with pytest.raises(TypeError):
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
    # A different code path claims with a spoofed identity: the sender can no longer claim.
    spoof = LeaseIdentity("spoof-machine", "spoof-boot", "spoof-ns", 4_000_000, 1)
    await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, spoof
    )
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await _dispatch(client, ledger, row, ref)
    assert broker.requests == []
    assert (await ledger.get(row["id"]))["state"] == "claimed"


@pytest.mark.asyncio
async def test_r2_b1_unreadable_host_identity_refuses_before_claim(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "r2-b1-noid")
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)

    def unreadable() -> LeaseIdentity:
        raise OSError("lease host identity unavailable")

    monkeypatch.setattr(client_module, "current_lease_identity", unreadable)
    client = await client_for("r2-b1-noid")
    with pytest.raises(OSError):
        await _dispatch(client, ledger, row, ref)
    stored = await ledger.get(row["id"])
    assert (
        broker.requests == []
        and stored["state"] == "intent"
        and stored["claim_token"] is None
    )


# ------------------------------------------------------------------ B2
def test_r2_b2_allowlist_cannot_be_hand_made_replaced_or_copied_into_new_data() -> None:
    verified = MockAccountAllowlist.from_acctinfo_response(
        payload={"Output_0": [{"acct_no": "MOCK-r2-real", "acct_type": "03"}]},
        configured_account_no="MOCK-r2-real",
    )
    with pytest.raises(NHPlugMockAccountRejected):
        MockAccountAllowlist(
            configured_account_no="LIVE-1",
            allowed_account_numbers=frozenset({"LIVE-1"}),
            account_type_counts=(("03", 1),),
        )
    with pytest.raises(NHPlugMockAccountRejected):
        dataclasses.replace(
            verified,
            configured_account_no="LIVE-1",
            allowed_account_numbers=frozenset({"LIVE-1"}),
        )
    for wrong_token in (None, object(), 0, "token"):
        with pytest.raises(NHPlugMockAccountRejected):
            MockAccountAllowlist(
                configured_account_no="LIVE-1",
                allowed_account_numbers=frozenset({"LIVE-1"}),
                account_type_counts=(),
                _factory_token=wrong_token,
            )
    clone = copy.copy(verified)
    assert clone.configured_account_no == "MOCK-r2-real"
    for types in (["01"], ["02"], ["03", "01"], ["03", "02"]):
        rows = [{"acct_no": "MOCK-r2-conflict", "acct_type": t} for t in types]
        with pytest.raises((NHPlugMockAccountRejected, NHPlugMockResponseError)):
            MockAccountAllowlist.from_acctinfo_response(
                payload={"Output_0": rows}, configured_account_no="MOCK-r2-conflict"
            )


@pytest.mark.asyncio
async def test_r2_b2_only_this_clients_verified_03_account_reaches_fake_send(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    ledger = NHPlugMockLedger(seeded_engine)

    async def row_for(act_no: str, key: str) -> tuple[dict[str, Any], UUID]:
        ref = await resolve_account_ref(seeded_engine, act_no, {1: KEY})
        row, _ = await ledger.create_intent(
            OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref),
            readiness=READY,
            idempotency_key=key,
            order_date=date.today(),
        )
        return row, ref

    fabricated = MockAccountAllowlist.from_acctinfo_response(
        payload={"Output_0": [{"acct_no": "FABRICATED-03", "acct_type": "03"}]},
        configured_account_no="FABRICATED-03",
    )
    fab_row, fab_ref = await row_for("FABRICATED-03", "r2_b2_fabricated_1234")
    # 1. A caller-bound (parsed but not fetched by this client) allowlist enables reads only.
    unverified = _client_with_accounts([{"acct_no": "MOCK-r2-a", "acct_type": "03"}])
    unverified.bind_account_allowlist(fabricated)
    with pytest.raises(LedgerConflict, match="broker_account_verification_required"):
        await _dispatch(unverified, ledger, fab_row, fab_ref)
    # 2. Verification replaces the caller binding with this client's own 03 account.
    await unverified.verify_and_bind_mock_account("MOCK-r2-a")
    with pytest.raises(LedgerConflict, match="account_ref_mismatch"):
        await _dispatch(unverified, ledger, fab_row, fab_ref)
    # 3. No post-verification rebinding and no second verification of another account.
    with pytest.raises(NHPlugMockConfigurationError, match="cannot be replaced"):
        unverified.bind_account_allowlist(fabricated)
    two = _client_with_accounts(
        [
            {"acct_no": "MOCK-r2-a", "acct_type": "03"},
            {"acct_no": "MOCK-r2-b", "acct_type": "03"},
        ]
    )
    await two.verify_and_bind_mock_account("MOCK-r2-a")
    with pytest.raises(NHPlugMockConfigurationError, match="cannot be replaced"):
        await two.verify_and_bind_mock_account("MOCK-r2-b")
    # 4. Conflicting or live account types never verify.
    for rows in (
        [
            {"acct_no": "MOCK-r2-c", "acct_type": "03"},
            {"acct_no": "MOCK-r2-c", "acct_type": "01"},
        ],
        [{"acct_no": "MOCK-r2-c", "acct_type": "01"}],
        [{"acct_no": "MOCK-r2-c", "acct_type": "02"}],
    ):
        conflicted = _client_with_accounts(rows)
        with pytest.raises((NHPlugMockAccountRejected, NHPlugMockResponseError)):
            await conflicted.verify_and_bind_mock_account("MOCK-r2-c")
        c_row, c_ref = await row_for("MOCK-r2-c", "r2_b2_conflict_" + uuid4().hex[:10])
        with pytest.raises(
            LedgerConflict, match="broker_account_verification_required"
        ):
            await _dispatch(conflicted, ledger, c_row, c_ref)
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE review.nhplug_mock_order_ledger SET state='withdrawn', withdraw_reason='probe' WHERE id=:id"
                ),
                {"id": c_row["id"]},
            )
    # 5. A second client verified for account B cannot send account A's row.
    a_row, a_ref = await row_for("MOCK-r2-a", "r2_b2_account_a_12345")
    client_b = _client_with_accounts([{"acct_no": "MOCK-r2-b", "acct_type": "03"}])
    await client_b.verify_and_bind_mock_account("MOCK-r2-b")
    with pytest.raises(LedgerConflict, match="account_ref_mismatch"):
        await _dispatch(client_b, ledger, a_row, a_ref)
    assert broker.requests == []
    # 6. The verified owner sends exactly its own account.
    await _dispatch(unverified, ledger, a_row, a_ref)
    assert [body["Input_0"]["act_no"] for _, body in broker.requests] == ["MOCK-r2-a"]
