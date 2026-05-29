"""ROB-341 — holdings/cash-delta fill-confirmation tests.

Covers the pure delta kernel (shared with the ROB-102 reconciler), the
cash-delta fill-price derivation, and the fail-closed async confirm
orchestration. stdlib + fakes only; no broker / network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

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
