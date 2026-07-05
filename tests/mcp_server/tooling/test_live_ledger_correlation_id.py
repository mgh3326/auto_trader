import pytest

from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "model", [KISLiveOrderLedger, LiveOrderLedger, TossLiveOrderLedger]
)
def test_correlation_id_column_present_and_nullable(model):
    col = model.__table__.c.correlation_id
    assert col is not None
    assert col.nullable is True
    # indexed for join lookups
    index_cols = {
        tuple(c.name for c in idx.columns) for idx in model.__table__.indexes
    }
    assert ("correlation_id",) in index_cols
