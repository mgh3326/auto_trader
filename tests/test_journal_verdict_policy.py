"""ROB-405 Slice B — verdict policy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.trade_journal.journal_verdict_policy import (
    classify_journal_verdict,
)


@pytest.mark.parametrize(
    "pnl_pct,expected",
    [
        (Decimal("2.0"), "good"),
        (Decimal("1.0"), "good"),  # boundary inclusive
        (Decimal("0.5"), "neutral"),
        (Decimal("-0.5"), "neutral"),
        (Decimal("-1.0"), "bad"),  # boundary inclusive
        (Decimal("-2.0"), "bad"),
        (None, "neutral"),
    ],
)
def test_classify_journal_verdict(pnl_pct, expected):
    assert classify_journal_verdict(pnl_pct) == expected
