"""ROB-403 — clause/condition evaluation."""

from __future__ import annotations

import pytest

from app.jobs.watch_market_data import evaluate_clause


@pytest.mark.parametrize(
    "current,clause,expected",
    [
        (100.0, {"metric": "price", "op": "above", "threshold": "90"}, True),
        (80.0, {"metric": "price", "op": "above", "threshold": "90"}, False),
        (80.0, {"metric": "price", "op": "below", "threshold": "90"}, True),
        (52.0, {"metric": "price", "op": "between", "low": "50", "high": "55"}, True),
        (60.0, {"metric": "price", "op": "between", "low": "50", "high": "55"}, False),
        (50.0, {"metric": "price", "op": "between", "low": "50", "high": "55"}, True),
        (None, {"metric": "price", "op": "above", "threshold": "90"}, False),
    ],
)
def test_evaluate_clause(current, clause, expected):
    assert evaluate_clause(current, clause) is expected
