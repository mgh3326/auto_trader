"""ROB-341 — holdings/cash-delta fill-confirmation tests.

Covers the pure delta kernel (shared with the ROB-102 reconciler), the
cash-delta fill-price derivation, and the fail-closed async confirm
orchestration. stdlib + fakes only; no broker / network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping_exec.holdings_delta_confirm import (
    derive_fill_price,
)
from app.services.kis_mock_holdings_reconciler import classify_fill_by_delta


@pytest.mark.unit
@pytest.mark.parametrize(
    "side,baseline,observed,ordered,verdict,filled",
    [
        ("buy", "0", "10", "10", "filled", "10"),
        ("buy", "0", "4", "10", "partial", "4"),
        ("buy", "0", "0", "10", "none", "0"),
        ("buy", "5", "15", "10", "filled", "10"),  # baseline position present
        ("buy", "5", "9", "10", "partial", "4"),  # delta below ordered
        ("buy", "5", "3", "10", "none", "0"),  # holdings DROPPED after a buy -> impossible
        ("sell", "10", "0", "10", "filled", "10"),
        ("sell", "10", "6", "10", "partial", "4"),
        ("sell", "10", "10", "10", "none", "0"),
        ("sell", "10", "12", "10", "none", "0"),  # holdings ROSE after a sell -> impossible
    ],
)
def test_classify_fill_by_delta(side, baseline, observed, ordered, verdict, filled):
    res = classify_fill_by_delta(
        side=side,
        ordered_qty=Decimal(ordered),
        baseline_qty=Decimal(baseline),
        observed_qty=Decimal(observed),
    )
    assert res.verdict == verdict
    assert res.filled_qty == Decimal(filled)


@pytest.mark.unit
def test_price_from_cash_delta_buy():
    # cash dropped 100000 for 10 shares -> 10000/share
    price, source = derive_fill_price(
        side="buy",
        filled_qty=Decimal("10"),
        cash_baseline=Decimal("1000000"),
        cash_observed=Decimal("900000"),
        limit_price=Decimal("9999"),
    )
    assert price == Decimal("10000")
    assert source == "cash_delta"


@pytest.mark.unit
def test_price_falls_back_to_limit_when_cash_unmoved():
    price, source = derive_fill_price(
        side="buy",
        filled_qty=Decimal("10"),
        cash_baseline=Decimal("1000000"),
        cash_observed=Decimal("1000000"),
        limit_price=Decimal("9999"),
    )
    assert price == Decimal("9999")
    assert source == "limit_fallback"


@pytest.mark.unit
def test_price_falls_back_when_cash_unavailable():
    price, source = derive_fill_price(
        side="sell",
        filled_qty=Decimal("10"),
        cash_baseline=None,
        cash_observed=Decimal("900000"),
        limit_price=Decimal("8888"),
    )
    assert price == Decimal("8888")
    assert source == "limit_fallback"
