from decimal import Decimal

import pytest

from app.services.paper_correlation import paper_correlation_id


def _id(**kw):
    base = {
        "account_id": 1,
        "symbol": "KRW-BTC",
        "side": "buy",
        "limit_price": Decimal("94000000"),
        "quantity": Decimal("0.001"),
        "kst_trade_day": "2026-07-05",
        "rung": 0,
    }
    base.update(kw)
    return paper_correlation_id(**base)


@pytest.mark.unit
def test_deterministic_same_inputs():
    assert _id() == _id()
    assert _id().startswith("paper:1:")


@pytest.mark.unit
def test_trade_day_salt_changes_id():
    assert _id(kst_trade_day="2026-07-05") != _id(kst_trade_day="2026-07-06")


@pytest.mark.unit
def test_rung_salt_changes_id():
    assert _id(rung=0) != _id(rung=1)


@pytest.mark.unit
def test_symbol_side_price_qty_change_id():
    assert _id(symbol="KRW-ETH") != _id()
    assert _id(side="sell") != _id()
    assert _id(limit_price=Decimal("94000001")) != _id()
    assert _id(quantity=Decimal("0.002")) != _id()
