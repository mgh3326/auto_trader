from decimal import Decimal

import pytest

from app.services.live_correlation import live_correlation_id


@pytest.mark.unit
def test_stable_and_namespaced():
    kwargs = dict(
        account_scope="kis_live",
        symbol="AAPL",
        side="buy",
        price=Decimal("190.50"),
        quantity=Decimal("3"),
        kst_trade_day="2026-07-05",
    )
    a = live_correlation_id(**kwargs)
    b = live_correlation_id(**kwargs)
    assert a == b  # deterministic
    assert a.startswith("live:kis_live:")
    assert len(a.split(":")[-1]) == 16


@pytest.mark.unit
def test_symbol_case_insensitive_but_fields_and_rung_and_scope_vary():
    base = dict(
        account_scope="kis_live",
        symbol="aapl",
        side="buy",
        price=Decimal("190.50"),
        quantity=Decimal("3"),
        kst_trade_day="2026-07-05",
    )
    canon = live_correlation_id(**{**base, "symbol": "AAPL"})
    assert live_correlation_id(**base) == canon  # upper-cased internally
    assert live_correlation_id(**{**base, "side": "sell"}) != canon
    assert live_correlation_id(**{**base, "rung": 1}) != canon
    assert live_correlation_id(**{**base, "account_scope": "toss_live"}) != canon
    assert live_correlation_id(**{**base, "kst_trade_day": "2026-07-06"}) != canon
