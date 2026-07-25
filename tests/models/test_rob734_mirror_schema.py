# tests/models/test_rob734_mirror_schema.py
from app.models.review import KISMockOrderLedger, TradeRetrospective


def test_kis_mock_order_ledger_has_mirror_metadata_columns():
    cols = KISMockOrderLedger.__table__.columns
    assert "report_item_uuid" in cols
    assert "mirror_cohort" in cols
    assert "mirror_source_bucket" in cols
    assert cols["report_item_uuid"].nullable is True
    assert cols["mirror_cohort"].nullable is True
    assert cols["mirror_source_bucket"].nullable is True


def test_trade_retrospective_unique_key_is_correlation_plus_account_mode():
    constraints = {
        c.name: tuple(col.name for col in c.columns)
        for c in TradeRetrospective.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert constraints["uq_trade_retrospectives_correlation_account"] == (
        "correlation_id",
        "account_mode",
    )
    assert "uq_trade_retrospectives_correlation_id" not in constraints
