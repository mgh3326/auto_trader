from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import (
    TossLedgerIdempotencyConflict,
    TossLiveOrderLedgerService,
    parse_report_item_uuid,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
pytestmark.append(pytest.mark.usefixtures("toss_ledger_cleanup_lock"))


def _place_kwargs(**overrides):
    base = {
        "operation_kind": "place",
        "market": "us",
        "symbol": "AAPL",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "DAY",
        "quantity": Decimal("1"),
        "price": Decimal("190"),
        "order_amount": None,
        "currency": "USD",
        "client_order_id": "cid-default",
        "broker_order_id": "ord-default",
        "original_order_id": None,
        "status": "accepted",
        "broker_status": None,
        "response_code": "0",
        "response_message": None,
        "raw_response": {},
    }
    base.update(overrides)
    return base


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session, toss_ledger_cleanup_lock):
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


# ROB-545 B1 — report_item_uuid must never raise after a live POST is accepted.


async def test_parse_report_item_uuid_is_fail_open_for_malformed():
    assert parse_report_item_uuid("not-a-uuid") is None
    assert parse_report_item_uuid("") is None
    assert parse_report_item_uuid(None) is None
    valid = "11111111-1111-1111-1111-111111111111"
    assert str(parse_report_item_uuid(valid)) == valid


async def test_record_send_with_malformed_report_item_uuid_records_none(db_session):
    svc = TossLiveOrderLedgerService(db_session)

    row = await svc.record_send(
        **_place_kwargs(
            client_order_id="cid-malformed-uuid",
            broker_order_id="ord-malformed-uuid",
            report_item_uuid="definitely-not-a-uuid",
        )
    )

    assert row.id is not None
    assert row.report_item_uuid is None
    assert row.status == "accepted"


# ROB-545 B2 — record_send must be idempotent on client_order_id.


async def test_record_send_idempotent_replay_returns_existing_row(db_session):
    svc = TossLiveOrderLedgerService(db_session)

    first = await svc.record_send(
        **_place_kwargs(client_order_id="cid-idem", broker_order_id="ord-idem")
    )
    second = await svc.record_send(
        **_place_kwargs(client_order_id="cid-idem", broker_order_id="ord-idem")
    )

    assert second.id == first.id
    rows = (await db_session.execute(select(TossLiveOrderLedger))).scalars().all()
    assert len(rows) == 1


async def test_record_send_conflicting_broker_id_raises_idempotency_conflict(
    db_session,
):
    svc = TossLiveOrderLedgerService(db_session)

    await svc.record_send(
        **_place_kwargs(client_order_id="cid-anomaly", broker_order_id="ord-first")
    )

    with pytest.raises(TossLedgerIdempotencyConflict) as excinfo:
        await svc.record_send(
            **_place_kwargs(client_order_id="cid-anomaly", broker_order_id="ord-second")
        )

    assert excinfo.value.client_order_id == "cid-anomaly"
    assert excinfo.value.existing_broker_order_id == "ord-first"
    assert excinfo.value.new_broker_order_id == "ord-second"


async def test_mark_manual_review_sets_operator_visible_error(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    row = await svc.record_send(
        **_place_kwargs(
            client_order_id="cid-manual-review",
            broker_order_id="ord-manual-review",
        )
    )

    await svc.mark_manual_review(
        ledger_id=row.id,
        reason="reconcile failed; operator must verify Toss order detail",
        error={
            "type": "TossApiResponseError",
            "status_code": 403,
            "code": "non-json-response",
            "request_id": "ray-403",
            "message": "<html>Forbidden</html>",
        },
        broker_status=None,
    )

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed is not None
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True
    assert (
        refreshed.manual_review_reason
        == "reconcile failed; operator must verify Toss order detail"
    )
    assert refreshed.last_reconcile_error == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-403",
        "message": "<html>Forbidden</html>",
    }
    assert refreshed.broker_status is None
    assert refreshed.reconciled_at is not None


async def test_update_reconcile_outcome_records_us_fx_fields(db_session):
    svc = TossLiveOrderLedgerService(db_session)
    row = await svc.record_send(
        **_place_kwargs(client_order_id="cid-fx", broker_order_id="ord-fx")
    )

    await svc.update_reconcile_outcome(
        ledger_id=row.id,
        status="filled",
        broker_status="FILLED",
        buy_fx_rate=Decimal("1389.33"),
        sell_fx_rate=Decimal("1503.19"),
        fx_pnl_krw=Decimal("22772.00"),
        security_pnl_usd=Decimal("60.00"),
        security_pnl_krw=Decimal("90191.40"),
        total_pnl_krw=Decimal("112963.40"),
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.buy_fx_rate == Decimal("1389.33")
    assert refreshed.sell_fx_rate == Decimal("1503.19")
    assert refreshed.fx_pnl_krw == Decimal("22772.00")
    assert refreshed.fx_rate_source == "reconcile_spot"
    assert refreshed.fx_pnl_accuracy == "approximate"


# ROB-651 P6-A — record_send stores approval_hash on insert only;
# a replay with the same client_order_id must keep the original hash.


async def test_record_send_stores_approval_hash(db_session):
    svc = TossLiveOrderLedgerService(db_session)

    row = await svc.record_send(
        **_place_kwargs(
            client_order_id="cid-approval-hash",
            broker_order_id="ord-approval-hash",
        ),
        approval_hash="p6a-abc123abc123abc1",
    )

    assert row.approval_hash == "p6a-abc123abc123abc1"


async def test_record_send_replay_keeps_original_approval_hash(db_session):
    svc = TossLiveOrderLedgerService(db_session)

    common = _place_kwargs(
        client_order_id="cid-approval-replay",
        broker_order_id="ord-approval-replay",
    )
    first = await svc.record_send(**common, approval_hash="p6a-original00000000")
    replay = await svc.record_send(**common, approval_hash="p6a-different0000000")

    assert replay.id == first.id
    assert replay.approval_hash == "p6a-original00000000"
