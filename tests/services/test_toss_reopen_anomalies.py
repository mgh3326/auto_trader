from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


async def _anomaly(db_session, *, cid, error, filled=None, trade_id=None, market="kr"):
    row = TossLiveOrderLedger(
        trade_date=datetime(2026, 7, 1, tzinfo=UTC),
        broker="toss",
        account_mode="toss_live",
        operation_kind="place",
        market=market,
        symbol="034020",
        side="buy",
        order_type="limit",
        client_order_id=cid,
        broker_order_id=f"ord-{cid}",
        status="anomaly",
        requires_manual_review=True,
        manual_review_reason="parked",
        last_reconcile_error=error,
        filled_qty=filled,
        trade_id=trade_id,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_dry_run_lists_instrument_type_anomaly_without_mutating(db_session):
    row = await _anomaly(
        db_session,
        cid="a",
        error={
            "type": "ValueError",
            "message": "'equity' is not a valid InstrumentType",
        },
    )
    out = await TossLiveOrderLedgerService(db_session).reopen_anomalies_for_reconcile(
        dry_run=True
    )
    assert out["reopened"] == 0
    assert [c["ledger_id"] for c in out["candidates"]] == [row.id]

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"  # unchanged in dry run


async def test_apply_reopens_only_bug_signature_rows(db_session):
    good = await _anomaly(
        db_session,
        cid="good",
        error={
            "type": "ValueError",
            "message": "'equity' is not a valid InstrumentType",
        },
    )
    transient = await _anomaly(
        db_session,
        cid="transient",
        error={
            "type": "TossRateLimitError",
            "code": "rate-limit-exceeded",
            "status_code": 429,
        },
    )
    forbidden = await _anomaly(
        db_session,
        cid="forbidden",
        error={
            "type": "TossApiResponseError",
            "code": "non-json-response",
            "status_code": 403,
        },
    )
    with_fill = await _anomaly(
        db_session,
        cid="hasfill",
        error={
            "type": "ValueError",
            "message": "'equity' is not a valid InstrumentType",
        },
        filled=Decimal("3"),
        trade_id=99,
    )

    out = await TossLiveOrderLedgerService(db_session).reopen_anomalies_for_reconcile(
        dry_run=False
    )
    reopened_ids = {c["ledger_id"] for c in out["candidates"]}
    assert out["reopened"] == 2
    assert reopened_ids == {good.id, transient.id}

    for rid, expect in [
        (good.id, "accepted"),
        (transient.id, "accepted"),
        (forbidden.id, "anomaly"),  # 403 never blindly reopened
        (with_fill.id, "anomaly"),  # has fill evidence -> never reopened
    ]:
        r = await db_session.get(TossLiveOrderLedger, rid)
        assert r.status == expect
    reopened_good = await db_session.get(TossLiveOrderLedger, good.id)
    assert reopened_good.requires_manual_review is False
    assert reopened_good.manual_review_reason is None
    assert reopened_good.last_reconcile_error is None


async def test_market_filter_scopes_reopen(db_session):
    kr = await _anomaly(
        db_session,
        cid="kr",
        error={
            "type": "ValueError",
            "message": "'equity' is not a valid InstrumentType",
        },
        market="kr",
    )
    await _anomaly(
        db_session,
        cid="us",
        error={
            "type": "ValueError",
            "message": "'equity' is not a valid InstrumentType",
        },
        market="us",
    )
    out = await TossLiveOrderLedgerService(db_session).reopen_anomalies_for_reconcile(
        dry_run=False, market="kr"
    )
    assert {c["ledger_id"] for c in out["candidates"]} == {kr.id}
    assert out["reopened"] == 1
