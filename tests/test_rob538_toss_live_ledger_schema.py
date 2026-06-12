from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_toss_live_order_ledger_model_shape():
    from app.models.review import TossLiveOrderLedger

    assert TossLiveOrderLedger.__tablename__ == "toss_live_order_ledger"
    assert TossLiveOrderLedger.__table__.schema == "review"

    cols = set(TossLiveOrderLedger.__table__.columns.keys())
    for col in (
        "id",
        "trade_date",
        "client_order_id",
        "broker_order_id",
        "original_order_id",
        "replaced_by_order_id",
        "operation_kind",
        "market",
        "symbol",
        "side",
        "order_type",
        "time_in_force",
        "quantity",
        "price",
        "order_amount",
        "currency",
        "status",
        "broker_status",
        "filled_qty",
        "avg_fill_price",
        "commission",
        "tax",
        "settlement_date",
        "raw_response",
        "report_item_uuid",
        "trade_id",
        "journal_id",
        "reconciled_at",
    ):
        assert col in cols, f"missing column {col}"


def test_toss_live_order_ledger_is_exported():
    import app.models as models

    assert hasattr(models, "TossLiveOrderLedger")
