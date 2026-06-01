# tests/mcp_server/tooling/test_kis_live_ledger.py
import pytest

from app.models.review import KISLiveOrderLedger


@pytest.mark.unit
def test_kis_live_order_ledger_model_columns():

    assert KISLiveOrderLedger.__tablename__ == "kis_live_order_ledger"
    cols = {c.name for c in KISLiveOrderLedger.__table__.columns}
    # intent fields must persist so reconcile can build the journal later
    for required in (
        "order_no",
        "symbol",
        "instrument_type",
        "side",
        "order_type",
        "quantity",
        "price",
        "amount",
        "currency",
        "status",
        "lifecycle_state",
        "thesis",
        "strategy",
        "target_price",
        "stop_loss",
        "min_hold_days",
        "notes",
        "exit_reason",
        "reason",
        "filled_qty",
        "avg_fill_price",
        "trade_id",
        "journal_id",
    ):
        assert required in cols, required
    # order_no uniqueness so the same broker order can't double-book
    constraint_names = {c.name for c in KISLiveOrderLedger.__table__.constraints}
    assert "uq_kis_live_ledger_order_no" in constraint_names


@pytest.mark.unit
def test_derive_live_send_status():
    from app.mcp_server.tooling.kis_live_ledger import _derive_live_send_status

    # rt_cd == "0" -> accepted regardless of odno presence
    assert _derive_live_send_status(rt_cd="0", order_no="0006366300") == "accepted"
    # non-zero rt_cd -> rejected (broker evidence of failure, never fake success)
    assert _derive_live_send_status(rt_cd="40", order_no=None) == "rejected"
    # missing rt_cd but odno present -> accepted
    assert _derive_live_send_status(rt_cd=None, order_no="0006366300") == "accepted"
    # missing rt_cd and no odno -> unknown
    assert _derive_live_send_status(rt_cd=None, order_no=None) == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_kis_live_order_ledger_inserts_row(db_session):
    from app.mcp_server.tooling.kis_live_ledger import (
        _order_session_factory,
        _save_kis_live_order_ledger,
    )
    from sqlalchemy import select
    from app.models.review import KISLiveOrderLedger

    ledger_id = await _save_kis_live_order_ledger(
        symbol="035420",
        instrument_type="equity_kr",
        side="sell",
        order_type="limit",
        quantity=10.0,
        price=250000.0,
        amount=2500000.0,
        currency="KRW",
        order_no="TEST-0006366300",
        order_time="0925",
        krx_fwdg_ord_orgno="00950",
        status="accepted",
        response_code="0",
        response_message="정상처리",
        raw_response={"rt_cd": "0"},
        reason="rob395 test",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason="take_profit",
        indicators_snapshot=None,
    )
    assert ledger_id is not None

    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(
                    KISLiveOrderLedger.order_no == "TEST-0006366300"
                )
            )
        ).scalar_one()
    assert row.status == "accepted"
    assert row.lifecycle_state == "accepted"
    assert row.trade_id is None and row.journal_id is None


