import pytest

from app.services.investment_stages.budget import (
    BudgetExceeded,
    StageLLMBudget,
)


def test_budget_consumes_within_cap():
    b = StageLLMBudget(max_calls=4)
    for _ in range(4):
        b.consume("bull_reducer")
    assert b.remaining == 0


def test_budget_rejects_overshoot():
    b = StageLLMBudget(max_calls=2)
    b.consume("a")
    b.consume("b")
    with pytest.raises(BudgetExceeded):
        b.consume("c")
