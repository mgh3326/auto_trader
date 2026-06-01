# tests/mcp_server/tooling/test_kis_live_ledger.py
import pytest


@pytest.mark.unit
def test_kis_live_order_ledger_model_columns():
    from app.models.review import KISLiveOrderLedger

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

