# tests/test_rob653_order_intent_schema.py
from app.models.review import KISLiveOrderLedger, LiveOrderLedger, OrderSendIntent


def test_ledger_additive_columns_present():
    for model in (KISLiveOrderLedger, LiveOrderLedger):
        cols = model.__table__.columns
        assert "approval_hash" in cols
        assert "idempotency_key" in cols
        assert cols["approval_hash"].nullable
        assert cols["idempotency_key"].nullable


def test_order_send_intent_table_and_unique():
    t = OrderSendIntent.__table__
    assert t.name == "order_send_intents"
    assert t.schema == "review"
    names = {c.name for c in t.columns}
    assert {
        "id",
        "account_scope",
        "idempotency_key",
        "symbol",
        "side",
        "created_at",
    } <= names
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in t.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("account_scope", "idempotency_key") in uniques
