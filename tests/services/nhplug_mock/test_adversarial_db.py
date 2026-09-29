# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""PostgreSQL guard cases using legally reached order states.

The empty key-registry case must run before the v1 seed fixture.
"""

from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.nhplug_mock.account_identity import (
    AccountIdentityError,
    resolve_account_ref,
)
from app.services.nhplug_mock.intent import OrderIntent
from app.services.nhplug_mock.ledger import (
    DispatchOutcome,
    LedgerConflict,
    NHPlugMockLedger,
)
from tests.services.nhplug_mock.test_dispatch_state_machine import (  # noqa: F401
    IDENTITY,
    KEY,
    READY,
    account,
    listing,
    mock_gate,
    nhplug_engine,
    scoped,
    seeded_engine,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_probe_a_empty_key_registry_zero_writes(
    nhplug_engine: AsyncEngine,
) -> None:
    async with nhplug_engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_key_version")
            )
        ).scalar_one() == 0
        before = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    with pytest.raises(AccountIdentityError, match="key_registry_empty"):
        await resolve_account_ref(nhplug_engine, "MOCK-empty-registry", {1: KEY})
    async with nhplug_engine.connect() as conn:
        after = (
            await conn.execute(
                text("SELECT count(*) FROM review.nhplug_mock_account_ref")
            )
        ).scalar_one()
    assert after == before


async def _accepted(engine: AsyncEngine, suffix: str, number: str):
    ref = await account(engine, suffix)
    ledger = NHPlugMockLedger(engine)
    row, _ = await ledger.create_intent(
        OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref),
        readiness=READY,
        idempotency_key=f"{suffix}_first_12345678",
        order_date=date.today(),
    )
    claim = await ledger.claim(
        row["id"], row["client_request_id"], row["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", number)
    )
    assert await ledger.verify_own_number(
        row["id"], scoped(listing(int(number)), row), readiness=READY
    )
    return ledger, row, ref


@pytest.mark.asyncio
async def test_probe_b_second_order_needs_matching_one_use_operator_authorization(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, root, ref = await _accepted(seeded_engine, "t15", "501")
    same_body = OrderIntent("place", "buy", "005930", 1, 67400, None, None, ref)
    with pytest.raises(LedgerConflict, match="duplicate_order_requires_authorization"):
        await ledger.create_intent(
            same_body,
            readiness=READY,
            idempotency_key="t15_second_new_key_1",
            order_date=date.today(),
        )

    async def auth(kind: str, target: int, digest: str) -> object:
        auth_id = uuid4()
        async with seeded_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO review.nhplug_mock_operator_authorization "
                    "(id,kind,target_row_id,account_ref,order_date,body_digest,evidence,grace_until,operator_id,reason) "
                    "VALUES (:id,:kind,:t,:a,:d,:g,'{}'::jsonb,now(),'probe','probe')"
                ),
                {
                    "id": auth_id,
                    "kind": kind,
                    "t": target,
                    "a": ref,
                    "d": root["order_date"],
                    "g": digest,
                },
            )
        return auth_id

    wrong_kind = await auth("abandon", root["id"], root["body_digest"])
    wrong_target = await auth("second_order", root["id"] + 100000, root["body_digest"])
    for bad in (wrong_kind, wrong_target):
        with pytest.raises(Exception, match="authorization mismatch or consumed"):
            await ledger.create_intent(
                same_body,
                readiness=READY,
                idempotency_key="t15_second_bad_" + str(bad)[:8],
                order_date=date.today(),
                duplicate_of=root["id"],
                second_order_authorization_id=bad,
            )
    good = await auth("second_order", root["id"], root["body_digest"])
    second, should_claim = await ledger.create_intent(
        same_body,
        readiness=READY,
        idempotency_key="t15_second_good_12345",
        order_date=date.today(),
        duplicate_of=root["id"],
        second_order_authorization_id=good,
    )
    assert (
        should_claim
        and second["duplicate_ordinal"] == 1
        and second["duplicate_of"] == root["id"]
    )
    # One-use: the consumed authorization cannot mint a third order.
    claim = await ledger.claim(
        second["id"], second["client_request_id"], second["body_digest"], ref, IDENTITY
    )
    assert await ledger.fence(claim)
    assert await ledger.record_final(
        claim, DispatchOutcome("uncertain", "no_proof_code", "502")
    )
    assert await ledger.verify_own_number(
        second["id"], scoped(listing(502), second), readiness=READY
    )
    with pytest.raises((LedgerConflict, DBAPIError)):
        await ledger.create_intent(
            same_body,
            readiness=READY,
            idempotency_key="t15_third_reuse_12345",
            order_date=date.today(),
            duplicate_of=root["id"],
            second_order_authorization_id=good,
        )


@pytest.mark.asyncio
async def test_probe_c_bound_number_and_identity_are_immutable_after_legal_t9b(
    seeded_engine: AsyncEngine,
) -> None:
    ledger, row, ref = await _accepted(seeded_engine, "writeonce", "601")
    note = json.dumps({"account_ref": str(ref), "order_date": str(row["order_date"])})
    evidence = json.dumps({"x": 1})
    changes = {
        "rebind_number": "state='open', broker_order_id='456', ack_order_id='456'",
        "order_date": "order_date=order_date+1",
        "symbol": "symbol='000660'",
        "quantity": "quantity=2",
        "idempotency_key": "idempotency_key='changed_key_123456789'",
        "delete": None,
    }
    for name, assignment in changes.items():
        with pytest.raises(DBAPIError):
            async with seeded_engine.begin() as conn:
                if assignment is None:
                    await conn.execute(
                        text(
                            "DELETE FROM review.nhplug_mock_order_ledger WHERE id=:id"
                        ),
                        {"id": row["id"]},
                    )
                else:
                    await conn.execute(
                        text(
                            f"UPDATE review.nhplug_mock_order_ledger SET {assignment}, reconcile_state='verified', "
                            "filled_qty=0, open_qty=1, cancelled_qty=0, modified_qty=0, "
                            "evidence=CAST(:e AS jsonb), last_reconcile=CAST(:n AS jsonb) WHERE id=:id"
                            if name == "rebind_number"
                            else f"UPDATE review.nhplug_mock_order_ledger SET {assignment} WHERE id=:id"
                        ),
                        {"id": row["id"], "e": evidence, "n": note},
                    )
    final = await ledger.get(row["id"])
    assert final["state"] == "accepted" and final["broker_order_id"] == "601"
    assert final["symbol"] == "005930" and final["quantity"] == 1
