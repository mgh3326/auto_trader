# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""State, digest, deadline and rotation tests from design section 7.

Code-table and rotation cases run last because those registries are append-only.
"""

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

import httpcore
import httpcore._backends.anyio as anyio_backend_module
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.brokers.nhplug.client as client_module
import app.services.nhplug_mock.ledger as ledger_module
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
SUFFIX: dict[int, str] = {}
STATES = [
    "intent",
    "withdrawn",
    "claimed",
    "sending",
    "accepted",
    "rejected",
    "uncertain",
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "modified",
    "confirmed",
    "anomaly",
    "abandoned",
]
LEGAL = {
    ("intent", "withdrawn"),
    ("intent", "claimed"),
    ("claimed", "withdrawn"),
    ("claimed", "sending"),
    ("sending", "accepted"),
    ("sending", "rejected"),
    ("sending", "uncertain"),
    ("uncertain", "accepted"),
    ("uncertain", "anomaly"),
    ("uncertain", "abandoned"),
    ("accepted", "confirmed"),
    ("accepted", "anomaly"),
    ("open", "anomaly"),
    ("partially_filled", "anomaly"),
} | {
    (a, b)
    for a in ("accepted", "open", "partially_filled")
    for b in ("open", "partially_filled", "filled", "cancelled", "modified")
}


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
        dry_run=kw.pop("dry_run", False),
        confirm=kw.pop("confirm", True),
    )


async def _row(
    engine: AsyncEngine, suffix: str, intent: Any = None, day: date | None = None
):
    ref = await account(engine, suffix)
    ledger = NHPlugMockLedger(engine)
    intent = intent or OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref)
    intent = replace(intent, account_ref=ref)
    row, _ = await ledger.create_intent(
        intent,
        readiness=READY,
        idempotency_key=("k_" + suffix + "_" + uuid4().hex)[:60],
        order_date=day or date.today(),
    )
    SUFFIX[row["id"]] = suffix
    return ledger, row, ref


async def _uncertain(engine, suffix, number=None, intent=None, lease=60):
    ledger, row, ref = await _row(engine, suffix, intent)
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim, lease_seconds=lease)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", number)
    )
    return ledger, row, ref, claim


def _wire(
    status: int = 200,
    body: Any = None,
    raw: bytes | None = None,
    exc: BaseException | None = None,
):
    seen: list[tuple[str, dict[str, Any]]] = []

    class Wire(httpx.AsyncBaseTransport):
        def arm(self, deadline: float) -> None:
            return None

        async def hard_close(self, timeout: float) -> None:
            return None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            if exc is not None:
                raise exc
            if raw is not None:
                return httpx.Response(status, content=raw, request=request)
            return httpx.Response(
                status, json=body if body is not None else {}, request=request
            )

    return Wire, seen


# ---------------------------------------------------------------- T-SM-1 (illegal pairs)
def _payload(target: str) -> str:
    ev = (
        "'"
        + json.dumps(
            {
                "listing_order_id": "123",
                "listing_complete": True,
                "listing_scope": "all",
                "attributes_match": True,
            }
        )
        + "'::jsonb"
    )
    common = (
        "lease_closed_at=coalesce(lease_closed_at,now()), dispatcher_done_at=coalesce(dispatcher_done_at,now()), "
        "sending_at=coalesce(sending_at,now()), lease_expires_at=coalesce(lease_expires_at,now()), "
        "claim_token=coalesce(claim_token,gen_random_uuid()), claimed_at=coalesce(claimed_at,now())"
    )
    ids = "broker_order_id=coalesce(broker_order_id,'123'), ack_order_id=coalesce(ack_order_id,'123'), ack_source=coalesce(ack_source,'own_evidence')"
    ver = f"reconcile_state='verified', evidence={ev}, last_reconcile={ev}"
    qty0 = "filled_qty=NULL, open_qty=NULL, cancelled_qty=NULL, modified_qty=NULL, applied_qty=NULL"
    return {
        "intent": "state='intent', claim_token=NULL, claimed_at=NULL, sending_at=NULL, lease_expires_at=NULL, lease_closed_at=NULL",
        "withdrawn": "state='withdrawn', withdraw_reason='forged', sending_at=NULL, lease_expires_at=NULL",
        "claimed": f"state='claimed', {common}, sending_at=NULL, lease_expires_at=NULL, lease_closed_at=NULL, claim_deadline=now()+interval '1 hour'",
        "sending": f"state='sending', {common}, lease_closed_at=NULL, broker_order_id=NULL, ack_order_id=NULL, ack_source=NULL, {qty0}",
        "accepted": f"state='accepted', {ids}, {ver}, {common}, {qty0}",
        "rejected": f"state='rejected', reject_rsp_cd='40310', broker_order_id=NULL, ack_order_id=NULL, ack_source=NULL, {common}, {qty0}",
        "uncertain": f"state='uncertain', uncertain_reason='forged', broker_order_id=NULL, ack_order_id=NULL, ack_source=NULL, {common}, {qty0}",
        "open": f"state='open', {ids}, {ver}, filled_qty=0, open_qty=quantity, cancelled_qty=0, modified_qty=0, {common}",
        "partially_filled": f"state='partially_filled', {ids}, {ver}, filled_qty=1, open_qty=quantity-1, cancelled_qty=0, modified_qty=0, {common}",
        "filled": f"state='filled', {ids}, {ver}, filled_qty=quantity, open_qty=0, cancelled_qty=0, modified_qty=0, {common}",
        "cancelled": f"state='cancelled', {ids}, {ver}, filled_qty=0, open_qty=0, cancelled_qty=quantity, modified_qty=0, {common}",
        "modified": f"state='modified', {ids}, {ver}, filled_qty=0, open_qty=0, cancelled_qty=0, modified_qty=quantity, successor_order_id='999', {common}",
        "confirmed": f"state='confirmed', {ids}, {ver}, applied_qty=1, filled_qty=NULL, open_qty=NULL, cancelled_qty=NULL, modified_qty=NULL, {common}",
        "anomaly": f"state='anomaly', requires_manual_review=true, manual_review_reason='forged', {ver}, {common}",
        "abandoned": f"state='abandoned', resolution_authorization_id=gen_random_uuid(), manual_review_reason='forged', {common}",
    }[target]


async def _reach(
    engine: AsyncEngine, state: str
) -> tuple[NHPlugMockLedger, dict[str, Any]]:
    """Reach each source state only through legal service transitions."""

    tag = "sm-" + state + "-" + uuid4().hex[:6]
    if state == "intent":
        ledger, row, _ = await _row(engine, tag)
        return ledger, row
    if state in {"withdrawn", "claimed", "sending"}:
        ledger, row, ref = await _row(engine, tag)
        claim = await ledger.claim(
            row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
        )
        if state == "withdrawn":
            assert await ledger.withdraw(claim, "pre_send_refusal")
        if state == "sending":
            assert await ledger.fence(claim)
        return ledger, row
    if state in {"uncertain", "anomaly"}:
        ledger, row, _, _ = await _uncertain(engine, tag, "311")
        if state == "anomaly":
            assert not await ledger.verify_own_number(
                row["id"], scoped(listing(311, price=1), row), readiness=READY
            )
        return ledger, row
    if state == "abandoned":
        ledger, row, ref = await _row(engine, tag)
        dead = LeaseIdentity("m", "b", "n", 4_000_000, 1)
        claim = await ledger.claim(
            row["id"], row["client_request_id"], row["body_digest"], ref, dead
        )
        assert await ledger.fence(claim, lease_seconds=1)
        assert await ledger.record_final(
            claim, DispatchOutcome("uncertain", "no_proof_code")
        )
        await asyncio.sleep(1.1)
        pending = await ledger.get(row["id"])
        auth = uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_operator_authorization (id,kind,target_row_id,account_ref,order_date,"
                    "body_digest,evidence,grace_until,operator_id,reason) VALUES (:id,'abandon',:t,:a,:d,:g,"
                    "CAST(:e AS jsonb),:gr,'probe','probe')"
                ),
                {
                    "e": json.dumps(
                        {
                            "process_gone": True,
                            "listing_complete": True,
                            "grace_elapsed": True,
                        }
                    ),
                    "id": auth,
                    "t": row["id"],
                    "a": ref,
                    "d": row["order_date"],
                    "g": row["body_digest"],
                    "gr": pending["lease_expires_at"] + timedelta(milliseconds=50),
                },
            )
        await asyncio.sleep(0.1)
        import app.services.nhplug_mock.lease_host as lease_host

        orig = lease_host.process_gone_on_lease_host
        lease_host.process_gone_on_lease_host = lambda identity: (
            True
        )  # identity above is a dead pid
        try:
            assert await ledger.abandon_with_authorization(
                row["id"],
                auth,
                scoped(OrderListing("all", True, (), pages=1), row),
                readiness=READY,
            )
        finally:
            lease_host.process_gone_on_lease_host = orig
        return ledger, row
    qty = 2 if state == "partially_filled" else 1
    intent = OrderIntent("place", "buy", "005930", qty, 67400, None, None, uuid4())
    ledger, row, ref, _ = await _uncertain(
        engine, tag, "4" + str(abs(hash(tag)) % 10**6), intent
    )
    number = int((await ledger.get(row["id"]))["ack_evidence_order_id"])
    base = OrderRow(
        number, "005930", qty, 0, qty, side="buy", order_price=Decimal(67400)
    )
    assert await ledger.verify_own_number(
        row["id"],
        scoped(OrderListing("all", True, (base,), pages=1), row),
        readiness=READY,
    )
    if state == "accepted":
        return ledger, row
    if state in {"open", "partially_filled", "filled"}:
        filled = {"open": 0, "partially_filled": 1, "filled": qty}[state]
        own = replace(
            base,
            filled_qty=filled,
            open_qty=qty - filled,
            avg_fill_price=Decimal(67400) if filled else None,
        )
        all_l = scoped(OrderListing("all", True, (own,), pages=1), row)
        open_l = scoped(
            OrderListing("open", True, (own,) if own.open_qty else (), pages=1), row
        )
        fill_l = (
            scoped(OrderListing("filled", True, (own,), pages=1), row)
            if filled
            else None
        )
        assert (
            await ledger.reconcile_bound(
                row["id"], all_l, open_l, fill_l, readiness=READY
            )
            == state
        )
        return ledger, row
    if state in {"cancelled", "confirmed"}:
        cancel = OrderIntent(
            "cancel",
            "buy",
            "005930",
            None,
            None,
            str(number),
            "full",
            row["account_ref"],
        )
        c_ledger = NHPlugMockLedger(engine)
        c_row, _ = await c_ledger.create_intent(
            cancel,
            readiness=READY,
            idempotency_key=("c_" + tag + uuid4().hex)[:60],
            order_date=date.today(),
        )
        claim = await c_ledger.claim(
            c_row["id"],
            c_row["client_request_id"],
            c_row["body_digest"],
            row["account_ref"],
            IDENTITY,
        )
        assert await c_ledger.fence(claim)
        c_no = number + 1
        assert await c_ledger.record_final(
            claim, DispatchOutcome("uncertain", "no_proof_code", str(c_no))
        )
        c_own = OrderRow(
            c_no,
            "005930",
            1,
            0,
            0,
            side="buy",
            original_order_no=number,
            cancelled_qty=1,
        )
        root = replace(base, open_qty=0, cancelled_qty=1)
        all_l = scoped(OrderListing("all", True, (root, c_own), pages=1), row)
        open_l = scoped(OrderListing("open", True, (), pages=1), row)
        assert await c_ledger.verify_own_number(
            c_row["id"],
            scoped(OrderListing("all", True, (root, c_own), pages=1), c_row),
            readiness=READY,
        )
        if state == "cancelled":
            assert (
                await ledger.reconcile_bound(
                    row["id"], all_l, open_l, None, readiness=READY
                )
                == "cancelled"
            )
            return ledger, row
        assert (
            await c_ledger.reconcile_bound(
                c_row["id"],
                scoped(OrderListing("all", True, (root, c_own), pages=1), c_row),
                scoped(OrderListing("open", True, (), pages=1), c_row),
                None,
                readiness=READY,
            )
            == "confirmed"
        )
        return c_ledger, c_row
    if state == "modified":
        mod = OrderIntent(
            "modify", "buy", "005930", 1, 67000, str(number), "full", row["account_ref"]
        )
        m_ledger = NHPlugMockLedger(engine)
        m_row, _ = await m_ledger.create_intent(
            mod,
            readiness=READY,
            idempotency_key=("m_" + tag + uuid4().hex)[:60],
            order_date=date.today(),
        )
        claim = await m_ledger.claim(
            m_row["id"],
            m_row["client_request_id"],
            m_row["body_digest"],
            row["account_ref"],
            IDENTITY,
        )
        assert await m_ledger.fence(claim)
        m_no = number + 1
        assert await m_ledger.record_final(
            claim, DispatchOutcome("uncertain", "no_proof_code", str(m_no))
        )
        succ = OrderRow(
            m_no,
            "005930",
            1,
            0,
            1,
            side="buy",
            original_order_no=number,
            order_price=Decimal(67000),
        )
        root = replace(base, open_qty=0, modified_qty=1)
        assert await m_ledger.verify_own_number(
            m_row["id"],
            scoped(OrderListing("all", True, (root, succ), pages=1), m_row),
            readiness=READY,
        )
        all_l = scoped(OrderListing("all", True, (root, succ), pages=1), row)
        open_l = scoped(OrderListing("open", True, (succ,), pages=1), row)
        assert (
            await ledger.reconcile_bound(
                row["id"], all_l, open_l, None, readiness=READY
            )
            == "modified"
        )
        return ledger, row
    raise AssertionError(state)


@pytest.mark.asyncio
async def test_r2_sm1_every_illegal_pair_is_refused_from_legally_reached_states(
    seeded_engine: AsyncEngine,
) -> None:
    refused = 0
    offenders: list[tuple[str, str, str]] = []
    for source in STATES:
        if source == "rejected":
            continue  # reachable only with a no-order proof code fixture (tested below)
        ledger, row = await _reach(seeded_engine, source)
        assert (await ledger.get(row["id"]))["state"] == source
        targets = [t for t in STATES if (source, t) not in LEGAL]
        for target in targets:
            assignment = (
                _payload(target)
                if target != source
                else "uncertain_reason='forged-self'"
            )
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
                    after = (
                        await conn.execute(
                            text(
                                "SELECT state, uncertain_reason FROM review.nhplug_mock_order_ledger WHERE id=:id"
                            ),
                            {"id": row["id"]},
                        )
                    ).one()
                    changed = after[0] != source or (
                        target == source and after[1] == "forged-self"
                    )
                    if changed:
                        offenders.append((source, target, after[0]))
                    else:
                        refused += 1
                finally:
                    await trans.rollback()
    assert offenders == [], offenders
    print("T-SM-1 illegal attempts refused:", refused)
    assert refused >= 150


# ---------------------------------------------------------------- T-DB-4/5/6/7/9, T-NULL-1
@pytest.mark.asyncio
async def test_r2_db_checks_on_legal_rows(seeded_engine: AsyncEngine) -> None:
    ledger, open_row = await _reach(seeded_engine, "open")
    _, acc = await _reach(seeded_engine, "accepted")
    _, sending = await _reach(seeded_engine, "sending")
    _, intent = await _reach(seeded_engine, "intent")

    def scope(row: dict[str, Any]) -> str:
        return (
            "last_reconcile='"
            + json.dumps(
                {
                    "account_ref": str(row["account_ref"]),
                    "order_date": str(row["order_date"]),
                }
            )
            + "'::jsonb"
        )

    good = f"reconcile_state='verified', evidence='{{}}'::jsonb, {scope(acc)}"
    attempts = {
        "T-DB-4 open qty change without verified": (
            open_row,
            "reconcile_state='unknown', filled_qty=1, open_qty=0",
        ),
        "T-DB-5 amend_scope NULL->value": (intent, "amend_scope='full'"),
        "T-DB-6 leave sending without lease_closed_at": (
            sending,
            "state='uncertain', uncertain_reason='x', dispatcher_done_at=now()",
        ),
        "T-DB-7 accepted ids NULL": (acc, "broker_order_id=NULL, ack_order_id=NULL"),
        "T-DB-7 ack differs": (acc, "ack_order_id='999'"),
        "T-DB-9 filled NULL qty": (
            acc,
            f"state='filled', {good}, filled_qty=NULL, open_qty=0, cancelled_qty=0, modified_qty=0",
        ),
        "T-DB-9 filled != quantity": (
            acc,
            f"state='filled', {good}, filled_qty=0, open_qty=0, cancelled_qty=1, modified_qty=0",
        ),
        "T-DB-9 sum mismatch": (
            acc,
            f"state='open', {good}, filled_qty=0, open_qty=5, cancelled_qty=0, modified_qty=0",
        ),
        "T-DB-9 cancelled with open qty": (
            acc,
            f"state='cancelled', {good}, filled_qty=0, open_qty=1, cancelled_qty=0, modified_qty=0",
        ),
        "T-DB-9 modified without successor": (
            acc,
            f"state='modified', {good}, filled_qty=0, open_qty=0, cancelled_qty=0, modified_qty=1",
        ),
        "T-DB-9 evidence NULL": (
            acc,
            f"state='filled', reconcile_state='verified', evidence=NULL, {scope(acc)}, filled_qty=1, open_qty=0, cancelled_qty=0, modified_qty=0",
        ),
        "T-DB-9 not verified": (
            acc,
            f"state='filled', reconcile_state='unknown', evidence='{{}}'::jsonb, {scope(acc)}, filled_qty=1, open_qty=0, cancelled_qty=0, modified_qty=0",
        ),
    }
    # Positive control: the same direct T11 with a consistent quantity set is accepted,
    # so each refusal above is the specific CHECK/guard, not a generic wall.
    async with seeded_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    f"UPDATE review.nhplug_mock_order_ledger SET state='filled', {good}, "
                    "filled_qty=1, open_qty=0, cancelled_qty=0, modified_qty=0 WHERE id=:id"
                ),
                {"id": acc["id"]},
            )
            assert (
                await conn.execute(
                    text(
                        "SELECT state FROM review.nhplug_mock_order_ledger WHERE id=:id"
                    ),
                    {"id": acc["id"]},
                )
            ).scalar_one() == "filled"
        finally:
            await trans.rollback()
    for name, (row, assignment) in attempts.items():
        expected = (await ledger.get(row["id"]))["state"]
        with pytest.raises(DBAPIError):
            async with seeded_engine.begin() as conn:
                await conn.execute(
                    text(
                        f"UPDATE review.nhplug_mock_order_ledger SET {assignment} WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
        assert (await ledger.get(row["id"]))["state"] == expected, name
    ref = await account(seeded_engine, "r2-null")
    for column in (
        "idempotency_key",
        "duplicate_ordinal",
        "account_ref",
        "attempt_no",
        "order_date",
        "symbol",
        "side",
    ):
        values = {
            "idempotency_key": "'null_probe_1234567890'",
            "duplicate_ordinal": "0",
            "account_ref": f"'{ref}'",
            "attempt_no": "1",
            "order_date": "current_date",
            "symbol": "'005930'",
            "side": "'buy'",
        }
        values[column] = "NULL"
        with pytest.raises(DBAPIError):
            async with seeded_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO review.nhplug_mock_order_ledger (client_request_id,account_ref,idempotency_key,attempt_no,"
                        "order_date,operation_kind,side,symbol,quantity,price,duplicate_ordinal) VALUES "
                        f"(gen_random_uuid(),{values['account_ref']},{values['idempotency_key']},{values['attempt_no']},"
                        f"{values['order_date']},'place',{values['side']},{values['symbol']},1,1,{values['duplicate_ordinal']})"
                    )
                )


# ---------------------------------------------------------------- T-DIGEST-2/3
def test_r2_digest_every_field_changes_digest_and_null_differs_from_empty() -> None:
    ref = UUID("3f2b8c1e-7a4d-4e6b-9c0a-5d1e2f3a4b5c")
    base = OrderIntent("modify", "buy", "005930", 1, 67000, "1000123", "full", ref)
    variants = [
        replace(base, side="sell"),
        replace(base, symbol="000660"),
        replace(base, quantity=2),
        replace(base, price=67001),
        replace(base, original_order_id="1000124"),
        replace(base, amend_scope="partial"),
        replace(base, account_ref=uuid4()),
        replace(base, operation_kind="cancel", price=None, quantity=None),
    ]
    digests = {body_digest(v) for v in variants}
    assert body_digest(base) not in digests and len(digests) == len(variants)
    from app.services.nhplug_mock.intent import _field

    assert _field("org", None) != _field("org", "")


@pytest.mark.asyncio
async def test_r2_digest_drift_in_either_encoder_sends_nothing(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    ledger, row, ref = await _row(seeded_engine, "r2-drift-client")
    client = await client_for("r2-drift-client")
    monkeypatch.setattr(client_module, "body_digest", lambda intent: "0" * 64)
    with pytest.raises(LedgerConflict, match="digest_mismatch"):
        await _dispatch(client, ledger, row, ref)
    assert (
        broker.requests == [] and (await ledger.get(row["id"]))["state"] == "withdrawn"
    )
    monkeypatch.undo()
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    for name in ("KEY", "TIME", "DB", "HOST", "VENDOR"):
        monkeypatch.setenv(f"NHPLUG_STAGE2_{name}_CONFIRMED", "true")
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    ledger2, row2, ref2 = await _row(seeded_engine, "r2-drift-ledger")
    client2 = await client_for("r2-drift-ledger")
    monkeypatch.setattr(ledger_module, "body_digest", lambda intent: "0" * 64)
    with pytest.raises(LedgerConflict, match="digest_mismatch"):
        await _dispatch(client2, ledger2, row2, ref2)
    assert broker.requests == [] and (await ledger2.get(row2["id"]))["state"] in {
        "intent",
        "withdrawn",
    }


# ---------------------------------------------------------------- T-CLAIM-1, T-BODY-3, T-HOST-1, T-GATE-1/3
@pytest.mark.asyncio
async def test_r2_claim_gate_host_and_body_refusals_send_nothing(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    ledger, row, ref = await _row(seeded_engine, "r2-refusals")
    client = await client_for("r2-refusals")
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await client.dispatch_claimed_order(
            ledger,
            10**9,
            row["client_request_id"],
            row["body_digest"],
            ref,
            keys={1: KEY},
            readiness=READY,
            timing=Stage2Timing(),
            dry_run=False,
            confirm=True,
        )

    class SubLedger(NHPlugMockLedger):
        pass

    class SubClient(NHPlugMockClient):
        pass

    with pytest.raises(LedgerConflict, match="dispatcher_or_ledger_invalid"):
        await _dispatch(client, SubLedger(seeded_engine), row, ref)
    sub = SubClient(
        app_key="t",
        app_secret="t",
        token_provider=client._token_provider,
        transport=client._transport,
    )
    with pytest.raises(LedgerConflict, match="dispatcher_or_ledger_invalid"):
        await _dispatch(sub, ledger, row, ref)
    for dry_run, confirm in (
        (True, True),
        (False, False),
        (0, True),
        (False, 1),
        ("false", "true"),
        (None, True),
    ):
        with pytest.raises(LedgerConflict, match="confirmation_required"):
            await _dispatch(client, ledger, row, ref, dry_run=dry_run, confirm=confirm)
    for value in ("false", "", "1", "yes"):
        monkeypatch.setenv("NHPLUG_MOCK_ENABLED", value)
        with pytest.raises(Exception, match="disabled"):
            await _dispatch(client, ledger, row, ref)
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    assert (await ledger.get(row["id"]))["state"] == "intent"
    # T-HOST-1: host/port/scheme drift after construction -> T4, no send.
    for base in (
        "https://evil.invalid:8443",
        "https://moapi.nhplug.com:9443",
        "http://moapi.nhplug.com:8443",
    ):
        ledger_h, row_h, ref_h = await _row(seeded_engine, "r2-host-" + uuid4().hex[:6])
        client_h = await client_for_account(seeded_engine, row_h)
        monkeypatch.setattr(client_h, "_base_url", base)
        with pytest.raises(Exception, match="allowlist|escaped"):
            await _dispatch(client_h, ledger_h, row_h, ref_h)
        assert (await ledger_h.get(row_h["id"]))["state"] == "withdrawn"
    assert broker.requests == []


@pytest.mark.parametrize("wrong_field", ["client_request_id", "body_digest"])
@pytest.mark.asyncio
async def test_claim_identity_condition_rejects_before_one_legitimate_send(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    wrong_field: str,
) -> None:
    suffix = "claim-condition-" + wrong_field
    ledger, row, ref = await intent_row(seeded_engine, suffix)
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for(suffix)
    request_id = (
        uuid4() if wrong_field == "client_request_id" else row["client_request_id"]
    )
    digest = "0" * 64 if wrong_field == "body_digest" else row["body_digest"]

    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await ledger.claim(row["id"], request_id, digest, ref, IDENTITY)
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await client.dispatch_claimed_order(
            ledger,
            row["id"],
            request_id,
            digest,
            ref,
            keys={1: KEY},
            readiness=READY,
            timing=Stage2Timing(),
            dry_run=False,
            confirm=True,
        )
    stored = await ledger.get(row["id"])
    assert (stored["state"], stored["claim_token"], broker.requests) == (
        "intent",
        None,
        [],
    )

    outcome = await _dispatch(client, ledger, row, ref)
    assert outcome.state == "uncertain"
    assert len(broker.requests) == 1
    stored = await ledger.get(row["id"])
    assert stored["state"] == "uncertain"


async def client_for_account(
    engine: AsyncEngine, row: dict[str, Any]
) -> NHPlugMockClient:
    return await client_for(SUFFIX[row["id"]])


@pytest.mark.parametrize(
    ("operation", "quantity", "price", "scope", "field", "value"),
    [
        ("modify", 1, 67000, "partial", "all_pat_dit_cd", "1"),
        ("modify", 1, 67000, "full", "cor_qty", 5),
        ("modify", 1, 67000, "full", "rmt_mkt_cd", "SOR"),
        ("cancel", 2, None, "partial", "cor_qty", 3),
        ("cancel", None, None, "full", "all_pat_dit_cd", "2"),
        ("place", 1, 67400, None, "orr_cnd_dit_cd", "02"),
        ("place", 1, 67400, None, "nmn_pr_tp_cd", "05"),
    ],
)
@pytest.mark.asyncio
async def test_r2_body3_post_build_field_changes_withdraw(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    quantity: Any,
    price: Any,
    scope: Any,
    field: str,
    value: Any,
) -> None:
    suffix = "r2-body3-" + uuid4().hex[:6]
    ref = await account(seeded_engine, suffix)
    intent = OrderIntent(
        operation,
        "buy",
        "005930",
        quantity,
        price,
        None if operation == "place" else "1000200",
        scope,
        ref,
    )
    ledger = NHPlugMockLedger(seeded_engine)
    row, _ = await ledger.create_intent(
        intent,
        readiness=READY,
        idempotency_key=("b3_" + suffix)[:60],
        order_date=date.today(),
    )
    original = client_module.build_body

    def changed(i: Any, act_no: str):
        path, body = original(i, act_no)
        body[field] = value
        return path, body

    monkeypatch.setattr(client_module, "build_body", changed)
    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for(suffix)
    with pytest.raises(LedgerConflict, match="order_body_differs_from_claim"):
        await _dispatch(client, ledger, row, ref)
    assert (
        broker.requests == [] and (await ledger.get(row["id"]))["state"] == "withdrawn"
    )


# ---------------------------------------------------------------- T-IDEM-3/4, T3 kill point
@pytest.mark.asyncio
async def test_r2_idem_new_key_blocked_in_each_reserving_state_and_concurrent_t1(
    seeded_engine: AsyncEngine,
) -> None:
    for state in ("intent", "claimed", "sending", "uncertain"):
        ledger, row = await _reach(seeded_engine, state)
        other = OrderIntent(
            "place", "buy", "005930", 3, 60000, None, None, row["account_ref"]
        )
        with pytest.raises(LedgerConflict, match="in_flight_order_exists"):
            await ledger.create_intent(
                other,
                readiness=READY,
                idempotency_key="r2_idem3_" + uuid4().hex[:20],
                order_date=date.today() + timedelta(days=2),
            )
    ref = await account(seeded_engine, "r2-idem4")
    ledger = NHPlugMockLedger(seeded_engine)

    async def t1(i: int):
        try:
            return await ledger.create_intent(
                OrderIntent("place", "buy", "005930", 1, 60000 + i, None, None, ref),
                readiness=READY,
                idempotency_key=f"r2_idem4_key_{i}_12345",
                order_date=date.today(),
            )
        except LedgerConflict as exc:
            return exc.code

    results = await asyncio.gather(*(t1(i) for i in range(6)))
    rows = [r for r in results if isinstance(r, tuple)]
    assert (
        len(rows) == 1
        and sorted(r for r in results if isinstance(r, str))
        == ["in_flight_order_exists"] * 5
    )


@pytest.mark.asyncio
async def test_r2_kill_after_t3_then_same_key_retry_sends_once(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await intent_row(seeded_engine, "r2-kill-t3")
    payload = {
        "row_id": row["id"],
        "request_id": str(row["client_request_id"]),
        "digest": row["body_digest"],
        "account_ref": str(ref),
        "machine": "w",
        "boot": "w",
        "namespace": "w",
        "mode": "claim_die",
    }
    env = os.environ.copy()
    validate_run_owned_database_url(seeded_engine.url)
    env["NHPLUG_TEST_WORKER_DB_URL"] = seeded_engine.url.render_as_string(
        hide_password=False
    )
    env["NHPLUG_TEST_WORKER_PAYLOAD"] = json.dumps(payload)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.services.nhplug_mock.claim_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[3]),
    )
    assert (await asyncio.wait_for(proc.stdout.readline(), 10)).strip() == b"ready"
    proc.stdin.write(b"go\n")
    await proc.stdin.drain()
    assert (await asyncio.wait_for(proc.stdout.readline(), 10)).strip() == b"claimed"
    assert await asyncio.wait_for(proc.wait(), 10) == 0
    assert (await ledger.get(row["id"]))["state"] == "claimed"
    broker = FakeBroker(number="931")
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    client = await client_for("r2-kill-t3")
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await _dispatch(client, ledger, row, ref)
    await asyncio.sleep(5.2)
    await ledger.recover_expired()
    assert (await ledger.get(row["id"]))["withdraw_reason"] == "claim_deadline_passed"
    retry, should_claim = await ledger.create_intent(
        OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref),
        readiness=READY,
        idempotency_key=row["idempotency_key"],
        order_date=row["order_date"],
    )
    assert should_claim and retry["attempt_no"] == 2 and retry["id"] != row["id"]
    await _dispatch(client, ledger, retry, ref)
    assert len(broker.requests) == 1


# ---------------------------------------------------------------- T-UNC-1/3/4, T-NEG-1 via dispatch
@pytest.mark.parametrize(
    ("label", "status", "body", "raw", "exc", "expect_number", "expect_reason"),
    [
        (
            "redirect",
            302,
            {"rsp_cd": "00000", "Output_0": {"mkt_orr_no": "811"}},
            None,
            None,
            "811",
            "no_proof_code",
        ),
        ("client-error", 400, {"rsp_cd": "40000"}, None, None, None, "no_proof_code"),
        ("server-error", 503, None, b"upstream", None, None, "unparsed_response"),
        ("not-json", 200, None, b"<html>ok</html>", None, None, "unparsed_response"),
        ("array", 200, None, b"[1,2]", None, None, "unparsed_response"),
        ("no-number", 200, {"rsp_cd": "00000"}, None, None, None, "no_proof_code"),
        (
            "negative",
            200,
            {"rsp_cd": "40310", "rsp_msg": "insufficient"},
            None,
            None,
            None,
            "no_proof_code",
        ),
        (
            "negative-with-number",
            200,
            {"rsp_cd": "40310", "Output_0": {"mkt_orr_no": "812"}},
            None,
            None,
            "812",
            "no_proof_code",
        ),
        (
            "reset",
            200,
            None,
            None,
            httpx.RemoteProtocolError("reset"),
            None,
            "RemoteProtocolError",
        ),
        ("unknown", 200, None, None, RuntimeError("boom"), None, "RuntimeError"),
    ],
)
@pytest.mark.asyncio
async def test_r2_post_fence_outcomes_are_uncertain_without_resend(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    status: int,
    body: Any,
    raw: Any,
    exc: Any,
    expect_number: Any,
    expect_reason: str,
) -> None:
    ledger, row, ref = await _row(seeded_engine, "r2-unc-" + label)
    wire, seen = _wire(status, body, raw, exc)
    monkeypatch.setattr(client_module, "GatedTransport", wire)
    client = await client_for_account(seeded_engine, row)
    thrown: Exception | None = None
    outcome = None
    try:
        outcome = await _dispatch(client, ledger, row, ref)
    except Exception as error:
        thrown = error
    stored = await ledger.get(row["id"])
    assert stored["state"] == "uncertain", (
        thrown,
        stored["state"],
    )
    assert thrown is None and outcome is not None and outcome.state == "uncertain"
    assert (
        stored["ack_evidence_order_id"] == expect_number
        and stored["uncertain_reason"] == expect_reason
    )
    with pytest.raises(LedgerConflict, match="claim_rejected"):
        await _dispatch(client, ledger, row, ref)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_r2_keyboard_interrupt_records_uncertain_then_reraises(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await _row(seeded_engine, "r2-kbi")
    wire, seen = _wire(exc=KeyboardInterrupt())
    monkeypatch.setattr(client_module, "GatedTransport", wire)
    client = await client_for_account(seeded_engine, row)
    with pytest.raises(KeyboardInterrupt):
        await _dispatch(client, ledger, row, ref)
    stored = await ledger.get(row["id"])
    assert (
        stored["state"] == "uncertain"
        and stored["uncertain_reason"] == "KeyboardInterrupt"
        and len(seen) == 1
    )


# ---------------------------------------------------------------- real GatedTransport: T-LEASE-3/8
def _install_backend(monkeypatch: pytest.MonkeyPatch, *, connect_delay: float = 0.0):
    log: list[tuple[str, bytes]] = []
    response = b'{"rsp_cd":"00000","Output_0":{"mkt_orr_no":"941"}}'
    wire = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(response)).encode()
        + b"\r\n\r\n"
        + response
    )

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self, tls: bool = False) -> None:
            self.tls = tls
            self.pending = False

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            if self.pending:
                self.pending = False
                return wire
            return b""

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            log.append(("app" if self.tls else "plain", buffer))
            self.pending = True

        async def aclose(self) -> None:
            return None

        async def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ):
            log.append(("tls", (server_hostname or "").encode()))
            return Stream(tls=True)

        def get_extra_info(self, info: str) -> Any:
            return None

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host: str, port: int, **kw: Any) -> Stream:
            assert (host, port) == ("moapi.nhplug.com", 8443)
            log.append(("connect", f"{host}:{port}".encode()))
            if connect_delay:
                await asyncio.sleep(connect_delay)
            return Stream()

        async def connect_unix_socket(self, path: str, **kw: Any):
            raise AssertionError("unix")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr(anyio_backend_module, "AnyIOBackend", Backend)
    return log


@pytest.mark.asyncio
async def test_r2_lease3_connect_delay_past_first_write_deadline_sends_nothing(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    allow_external_http: None,
) -> None:
    log = _install_backend(monkeypatch, connect_delay=1.4)
    ledger, row, ref = await _row(seeded_engine, "r2-lease3")
    client = await client_for_account(seeded_engine, row)
    outcome = await _dispatch(
        client,
        ledger,
        row,
        ref,
        timing=Stage2Timing(first_write_seconds=1, lease_seconds=5),
    )
    stored = await ledger.get(row["id"])
    assert [k for k, _ in log if k in {"app", "plain"}] == [], log
    assert ("connect", b"moapi.nhplug.com:8443") in log
    assert outcome.state == "uncertain" and stored["state"] == "uncertain"
    assert stored["uncertain_reason"] == "FirstWriteDeadlineExceeded"


@pytest.mark.asyncio
async def test_r2_real_gate_on_time_sends_exactly_one_order_body(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    allow_external_http: None,
) -> None:
    log = _install_backend(monkeypatch)
    ledger, row, ref = await _row(seeded_engine, "r2-realgate")
    client = await client_for_account(seeded_engine, row)
    outcome = await _dispatch(client, ledger, row, ref)
    posts = [b for k, b in log if k == "app" and b.startswith(b"POST ")]
    assert len(posts) == 1 and posts[0].startswith(
        b"POST /krstock/order/v1/cashBuy HTTP/1.1"
    )
    assert outcome.state == "uncertain" and outcome.evidence_order_id == "941"


@pytest.mark.asyncio
async def test_r2_lease8_fence_lock_wait_past_deadline_sends_nothing_and_lock_timeout_never_sends(
    seeded_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    allow_external_http: None,
) -> None:
    log = _install_backend(monkeypatch)
    ledger, row, ref = await _row(seeded_engine, "r2-lease8")
    client = await client_for_account(seeded_engine, row)
    original_fence = ledger.fence

    async def fence_after_foreign_lock(claim: Any, **kw: Any) -> bool:
        conn = await seeded_engine.connect()
        trans = await conn.begin()
        await conn.execute(
            text(
                "SELECT 1 FROM review.nhplug_mock_order_ledger WHERE id=:id FOR UPDATE"
            ),
            {"id": claim.row_id},
        )

        async def release() -> None:
            await asyncio.sleep(1.5)
            await trans.rollback()
            await conn.close()

        task = asyncio.create_task(release())
        try:
            return await original_fence(claim, **kw)
        finally:
            await task

    monkeypatch.setattr(ledger, "fence", fence_after_foreign_lock)
    outcome = await _dispatch(
        client,
        ledger,
        row,
        ref,
        timing=Stage2Timing(first_write_seconds=1, lease_seconds=5),
    )
    stored = await ledger.get(row["id"])
    assert [k for k, _ in log if k in {"app", "plain"}] == []
    assert (
        outcome.state == "uncertain"
        and stored["uncertain_reason"] == "FirstWriteDeadlineExceeded"
    )
    # lock_timeout exceeded: no commit, no send, row stays claimed.
    ledger2, row2, ref2 = await _row(seeded_engine, "r2-lease8b")
    client2 = await client_for_account(seeded_engine, row2)
    original2 = ledger2.fence

    async def fence_blocked(claim: Any, **kw: Any) -> bool:
        conn = await seeded_engine.connect()
        trans = await conn.begin()
        await conn.execute(
            text(
                "SELECT 1 FROM review.nhplug_mock_order_ledger WHERE id=:id FOR UPDATE"
            ),
            {"id": claim.row_id},
        )
        try:
            return await original2(claim, **kw)
        finally:
            await trans.rollback()
            await conn.close()

    monkeypatch.setattr(ledger2, "fence", fence_blocked)
    with pytest.raises(Exception, match="lock|timeout|cancel"):
        await _dispatch(
            client2, ledger2, row2, ref2, timing=Stage2Timing(lock_timeout_ms=300)
        )
    assert (await ledger2.get(row2["id"]))["state"] == "claimed"
    assert [k for k, _ in log if k in {"app", "plain"}] == []


# ---------------------------------------------------------------- T-LEASE-7
@pytest.mark.asyncio
async def test_r2_lease7_end_to_end_deadline_with_concurrent_t8v_records_once(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, row, ref = await _row(seeded_engine, "r2-lease7")
    seen: list[str] = []

    class Slow(httpx.AsyncBaseTransport):
        def arm(self, deadline: float) -> None:
            return None

        async def hard_close(self, timeout: float) -> None:
            return None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    monkeypatch.setattr(client_module, "GatedTransport", Slow)
    client = await client_for_account(seeded_engine, row)

    async def recover_midway() -> None:
        await asyncio.sleep(3.4)
        assert (await ledger.recover_expired())[2] >= 1

    rec = asyncio.create_task(recover_midway())
    outcome = await _dispatch(
        client, ledger, row, ref, timing=Stage2Timing(lease_seconds=3, send_seconds=5)
    )
    await rec
    stored = await ledger.get(row["id"])
    assert outcome.state == "uncertain" and outcome.reason == "TimeoutError"
    assert (
        stored["state"] == "uncertain"
        and stored["uncertain_reason"] == "lease_expired_without_result"
    )
    assert stored["dispatcher_done_at"] is not None and len(seen) == 1


# ---------------------------------------------------------------- code-table fixtures T-SUCC-1, T-LEASE-5, T7 (runs last)
@pytest.mark.asyncio
async def test_r2_zzz_code_fixtures_are_path_scoped_t6_t9a_t7(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with seeded_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_success_proof_code(path,rsp_cd,citation,approved_by) VALUES "
                "('/krstock/order/v1/cashSell','00000','probe fixture only','r2-probe')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_no_order_proof_code(path,rsp_cd,citation,approved_by) VALUES "
                "('/krstock/order/v1/cancel','40310','probe fixture only','r2-probe')"
            )
        )
    ok = {"rsp_cd": "00000", "Output_0": {"mkt_orr_no": "951"}}
    # T6 on the fixture path.
    ledger, row, ref = await _row(
        seeded_engine,
        "r2-t6",
        OrderIntent("place", "sell", "005930", 1, 71000, None, None, uuid4()),
    )
    wire, seen = _wire(200, ok)
    monkeypatch.setattr(client_module, "GatedTransport", wire)
    client = await client_for_account(seeded_engine, row)
    outcome = await _dispatch(client, ledger, row, ref)
    stored = await ledger.get(row["id"])
    assert (
        outcome.state == "accepted"
        and stored["state"] == "accepted"
        and stored["ack_source"] == "response"
    )
    assert (
        stored["success_rsp_cd"] == "00000"
        and stored["broker_order_id"] == "951"
        and len(seen) == 1
    )
    # Same response on the buy path (no code) stays uncertain.
    ledger_b, row_b, ref_b = await _row(seeded_engine, "r2-t6-buy")
    wire_b, seen_b = _wire(200, ok)
    monkeypatch.setattr(client_module, "GatedTransport", wire_b)
    client_b = await client_for_account(seeded_engine, row_b)
    assert (await _dispatch(client_b, ledger_b, row_b, ref_b)).state == "uncertain"
    # T9a: recovery first, then the late proven result.
    ledger9, row9, ref9 = await _row(
        seeded_engine,
        "r2-t9a",
        OrderIntent("place", "sell", "000660", 1, 71000, None, None, uuid4()),
    )
    claim = await ledger9.claim(
        row9["id"], row9["client_request_id"], row9["body_digest"], ref9, IDENTITY
    )
    assert await ledger9.fence(claim, lease_seconds=1)
    await asyncio.sleep(1.1)
    await ledger9.recover_expired()
    assert await ledger9.record_final(
        claim, DispatchOutcome("accepted", evidence_order_id="952", rsp_cd="00000")
    )
    s9 = await ledger9.get(row9["id"])
    assert (
        s9["state"] == "accepted"
        and s9["ack_source"] == "response"
        and s9["dispatcher_done_at"] is not None
    )
    # T7 only with the documented no-order code and no number; with a number it stays uncertain.
    for number, expect in ((None, "rejected"), ("953", "uncertain")):
        cancel = OrderIntent(
            "cancel", "buy", "035420", None, None, "1000300", "full", uuid4()
        )
        ledger7, row7, ref7 = await _row(seeded_engine, "r2-t7-" + expect, cancel)
        body = {"rsp_cd": "40310"} | (
            {"Output_0": {"mkt_orr_no": number}} if number else {}
        )
        wire7, seen7 = _wire(200, body)
        monkeypatch.setattr(client_module, "GatedTransport", wire7)
        client7 = await client_for_account(seeded_engine, row7)
        await _dispatch(client7, ledger7, row7, ref7)
        s7 = await ledger7.get(row7["id"])
        assert s7["state"] == expect and len(seen7) == 1
        if expect == "rejected":
            assert s7["reject_rsp_cd"] == "40310" and s7["lease_closed_at"] is not None
            for target in STATES:
                assignment = (
                    _payload(target)
                    if target != "rejected"
                    else "uncertain_reason='forged-self'"
                )
                with pytest.raises(DBAPIError):
                    async with seeded_engine.begin() as conn:
                        await conn.execute(
                            text(
                                f"UPDATE review.nhplug_mock_order_ledger SET {assignment} WHERE id=:id"
                            ),
                            {"id": row7["id"]},
                        )
                assert (await ledger7.get(row7["id"]))["state"] == "rejected", target


# ---------------------------------------------------------------- key rotation T-ROT-2/3 (runs late)


@pytest.mark.parametrize("value", ["TRUE", "True", " true", "true "])
@pytest.mark.asyncio
async def test_r2_gate1_mock_enabled_requires_exact_lowercase_true(
    seeded_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Design section 7.7 T-GATE-1 and 5.1: NHPLUG_MOCK_ENABLED exactly "true"."""

    broker = FakeBroker()
    monkeypatch.setattr(client_module, "GatedTransport", broker.transport)
    ledger, row, ref = await _row(seeded_engine, "r2-gate1-" + uuid4().hex[:6])
    client = await client_for_account(seeded_engine, row)
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", value)
    try:
        await _dispatch(client, ledger, row, ref)
    except Exception:
        pass
    assert broker.requests == [], (repr(value), (await ledger.get(row["id"]))["state"])


@pytest.mark.parametrize("value", ["TRUE", "True", " true"])
def test_r2_stage2_flags_are_exact(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from app.services.nhplug_mock.readiness import Stage2Disabled, Stage2Readiness

    monkeypatch.setenv("NHPLUG_STAGE2_VENDOR_CONFIRMED", value)
    with pytest.raises(Stage2Disabled, match="vendor_unconfirmed"):
        Stage2Readiness.from_env().assert_ready()


@pytest.mark.asyncio
async def test_r2_zz_rotation_conflict_mixed_version_race_and_lock_wait(
    seeded_engine: AsyncEngine,
) -> None:
    v2 = KeyMaterial(2, "r2-probe-key-v2", b"r2-second-probe-key")
    async with seeded_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_key_version(key_version,key_id,key_check) VALUES (2,:i,:c)"
            ),
            {"i": v2.key_id, "c": v2.check()},
        )
    keys = {1: KEY, 2: v2}
    refs = await asyncio.gather(
        *(
            resolve_account_ref(seeded_engine, "MOCK-r2-mixed-race", keys)
            for _ in range(8)
        )
    )
    assert len(set(refs)) == 1
    async with seeded_engine.connect() as conn:
        bound = (
            await conn.execute(
                text(
                    "SELECT key_version, account_ref FROM review.nhplug_mock_account_binding WHERE account_ref=:r ORDER BY 1"
                ),
                {"r": refs[0]},
            )
        ).all()
    assert [(v, UUID(str(r))) for v, r in bound] == [(1, refs[0]), (2, refs[0])]
    before = None
    async with seeded_engine.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    with pytest.raises(AccountIdentityError, match="key_version_unavailable"):
        await resolve_account_ref(seeded_engine, "MOCK-r2-v1-only", {1: KEY})
    async with seeded_engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one() == before
    # Two retained versions pointing at different refs -> fail closed.
    other = uuid4()
    async with seeded_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO review.nhplug_mock_account_ref(account_ref) VALUES (:r)"),
            {"r": other},
        )
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_account_binding(key_version,binding,account_ref) VALUES (1,:b,:r)"
            ),
            {"b": KEY.binding("MOCK-r2-split"), "r": other},
        )
        split = uuid4()
        await conn.execute(
            text("INSERT INTO review.nhplug_mock_account_ref(account_ref) VALUES (:r)"),
            {"r": split},
        )
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_account_binding(key_version,binding,account_ref) VALUES (2,:b,:r)"
            ),
            {"b": v2.binding("MOCK-r2-split"), "r": split},
        )
    with pytest.raises(AccountIdentityError, match="account_binding_conflict"):
        await resolve_account_ref(seeded_engine, "MOCK-r2-split", keys)
    # A version insert waits while a resolver holds the shared registry lock.
    holder = await seeded_engine.connect()
    trans = await holder.begin()
    await holder.execute(text("SELECT pg_advisory_xact_lock_shared(711)"))
    try:
        v3 = KeyMaterial(3, "r2-probe-key-v3", b"r2-third-probe-key")
        with pytest.raises(Exception, match="lock timeout|canceling statement"):
            async with seeded_engine.begin() as conn:
                await conn.execute(text("SET LOCAL lock_timeout='400ms'"))
                await conn.execute(
                    text(
                        "INSERT INTO review.nhplug_mock_key_version(key_version,key_id,key_check) VALUES (3,:i,:c)"
                    ),
                    {"i": v3.key_id, "c": v3.check()},
                )
    finally:
        await trans.rollback()
        await holder.close()
