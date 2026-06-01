import pytest


@pytest.mark.unit
def test_live_order_ledger_model_shape():
    from app.models.review import LiveOrderLedger

    assert LiveOrderLedger.__tablename__ == "live_order_ledger"
    cols = set(LiveOrderLedger.__table__.columns.keys())
    # 디스크리미네이터 + 시장 메타가 존재
    for c in (
        "broker",
        "account_scope",
        "market",
        "symbol",
        "exchange",
        "market_symbol",
        "order_no",
        "order_kind",
        "status",
        "filled_qty",
        "avg_fill_price",
        "trade_id",
        "journal_id",
    ):
        assert c in cols, f"missing column {c}"
    assert LiveOrderLedger.__table__.schema == "review"
