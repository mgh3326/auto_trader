# tests/test_rob653_ledger_passthrough.py
import pytest
from sqlalchemy import select

from app.mcp_server.tooling.kis_live_ledger import _record_kis_live_order
from app.mcp_server.tooling.live_order_ledger import _record_live_order
from app.models.review import KISLiveOrderLedger, LiveOrderLedger


@pytest.mark.asyncio
async def test_kis_live_record_persists_hash_and_key(db_session):
    await _record_kis_live_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result={"price": 70000, "quantity": 10, "estimated_value": 700000},
        execution_result={"odno": "KISTEST653KR", "rt_cd": "0", "msg": "ok"},
        reason="t",
        exit_reason=None,
        thesis="t",
        strategy="t",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        approval_hash="p6a-deadbeef",
        idempotency_key="p6a-kr-key",
    )
    row = (
        await db_session.execute(
            select(KISLiveOrderLedger).where(
                KISLiveOrderLedger.order_no == "KISTEST653KR"
            )
        )
    ).scalar_one()
    assert row.approval_hash == "p6a-deadbeef"
    assert row.idempotency_key == "p6a-kr-key"


@pytest.mark.asyncio
async def test_live_order_record_persists_hash_and_key(db_session):
    await _record_live_order(
        broker="upbit",
        account_scope="upbit_live",
        market="crypto",
        normalized_symbol="BTC",
        exchange=None,
        market_symbol="KRW-BTC",
        side="buy",
        order_kind="limit",
        currency="KRW",
        order_no="UPBITTEST653",
        order_time=None,
        rt_cd="0",
        response_message=None,
        dry_run_result={"price": 1, "quantity": 1, "estimated_value": 1},
        execution_result={"uuid": "UPBITTEST653"},
        reason="t",
        exit_reason=None,
        thesis="t",
        strategy="t",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        approval_hash="p6a-cafe",
        idempotency_key="p6a-crypto-key",
    )
    row = (
        await db_session.execute(
            select(LiveOrderLedger).where(LiveOrderLedger.order_no == "UPBITTEST653")
        )
    ).scalar_one()
    assert row.approval_hash == "p6a-cafe"
    assert row.idempotency_key == "p6a-crypto-key"
