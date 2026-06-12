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


async def test_record_place_order_is_accepted_only(db_session):
    svc = TossLiveOrderLedgerService(db_session)

    row = await svc.record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190.5"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-1",
        broker_order_id="ord-1",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={"orderId": "ord-1"},
    )

    assert row.id is not None
    assert row.status == "accepted"
    assert row.filled_qty is None
    assert row.trade_id is None
    assert row.journal_id is None


async def test_mark_replaced_links_original_to_replacement(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    original = await svc.record_send(
        operation_kind="place",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-original",
        broker_order_id="ord-original",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )

    replacement = await svc.record_send(
        operation_kind="modify",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("70100"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-replacement",
        broker_order_id="ord-replacement",
        original_order_id="ord-original",
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    await svc.mark_replaced(
        broker_order_id="ord-original",
        replaced_by_order_id="ord-replacement",
    )

    refreshed = await db_session.get(TossLiveOrderLedger, original.id)
    assert refreshed is not None
    assert refreshed.replaced_by_order_id == replacement.broker_order_id
    assert refreshed.status == "accepted"


async def test_list_open_keeps_original_and_cancel_audit_row_reconcilable(
    db_session,
):
    svc = TossLiveOrderLedgerService(db_session)
    await svc.record_send(
        operation_kind="place",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-open-original",
        broker_order_id="ord-open-original",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    await svc.record_send(
        operation_kind="cancel",
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("1"),
        price=Decimal("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-cancel-audit",
        broker_order_id="ord-cancel-audit",
        original_order_id="ord-open-original",
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    await svc.mark_replaced(
        broker_order_id="ord-open-original",
        replaced_by_order_id="ord-cancel-audit",
    )

    rows = await svc.list_open(symbol="005930")

    assert [row.broker_order_id for row in rows] == [
        "ord-open-original",
        "ord-cancel-audit",
    ]


async def test_update_reconcile_outcome_records_fee_tax_and_settlement(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    row = await svc.record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-fill",
        broker_order_id="ord-fill",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )

    await svc.update_reconcile_outcome(
        ledger_id=row.id,
        status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_fill_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        settlement_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        trade_id=11,
        journal_id=22,
        raw_response={"status": "FILLED"},
    )

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed is not None
    assert refreshed.status == "filled"
    assert refreshed.filled_qty == Decimal("2")
    assert refreshed.commission == Decimal("0.05")
    assert refreshed.tax == Decimal("0.01")
    assert refreshed.trade_id == 11
    assert refreshed.journal_id == 22
