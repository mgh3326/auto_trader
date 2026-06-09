"""ROB-473 — 라이브 ledger save가 report_item_uuid를 기록한다."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_kis_live_save_records_report_item_uuid(db_session):
    from app.mcp_server.tooling import kis_live_ledger as m

    rid = uuid.uuid4()
    order_no = f"kis-{uuid.uuid4().hex[:10]}"
    ledger_id = await m._save_kis_live_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=70000.0,
        amount=70000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno="00950",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    assert ledger_id is not None
    from app.core.db import AsyncSessionLocal
    from app.models.review import KISLiveOrderLedger

    async with AsyncSessionLocal() as db:
        row = await db.get(KISLiveOrderLedger, ledger_id)
        assert row.report_item_uuid == rid


async def test_live_save_records_report_item_uuid(db_session):
    from app.mcp_server.tooling import live_order_ledger as m

    rid = uuid.uuid4()
    order_no = f"live-{uuid.uuid4().hex[:10]}"
    ledger_id = await m._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=1.0,
        price=200.0,
        amount=200.0,
        currency="USD",
        order_no=order_no,
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    from app.core.db import AsyncSessionLocal
    from app.models.review import LiveOrderLedger

    async with AsyncSessionLocal() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        assert row.report_item_uuid == rid


async def test_list_kis_live_orders_by_report_item_uuid(db_session):
    from app.mcp_server.tooling import kis_live_ledger as m

    rid = uuid.uuid4()
    order_no = f"rob473-{uuid.uuid4().hex[:10]}"
    await m._save_kis_live_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=70000.0,
        amount=70000.0,
        currency="KRW",
        order_no=order_no,
        order_time="090000",
        krx_fwdg_ord_orgno="00950",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    rows = await m.list_kis_live_orders_by_report_item_uuid(rid)
    assert any(r["order_no"] == order_no for r in rows)


async def test_list_live_orders_by_report_item_uuid(db_session):
    from app.mcp_server.tooling import live_order_ledger as m

    rid = uuid.uuid4()
    order_no = f"rob473-{uuid.uuid4().hex[:10]}"
    await m._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=1.0,
        price=200.0,
        amount=200.0,
        currency="USD",
        order_no=order_no,
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    rows = await m.list_live_orders_by_report_item_uuid(rid)
    assert any(r["order_no"] == order_no for r in rows)
