"""ROB-919: relative trade-value surge-ratio pure calculation tests.

Pure function, no DB access -- history rows are passed in already fetched.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.invest_momentum_events.surge_ratio import (
    compute_trade_value_surge_ratio,
)


def test_ratio_is_current_divided_by_average_of_history():
    result = compute_trade_value_surge_ratio(
        current_trade_value=Decimal("1000"),
        historical_trade_values=[
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
        ],
    )

    assert result.ratio == 10.0
    assert result.reason_code is None
    assert result.lookback_days_used == 3
    assert result.baseline_trade_value == 100.0


def test_none_historical_entries_are_excluded_from_average_not_treated_as_zero():
    result = compute_trade_value_surge_ratio(
        current_trade_value=Decimal("300"),
        historical_trade_values=[Decimal("100"), None, Decimal("100"), Decimal("100")],
    )

    assert result.lookback_days_used == 3
    assert result.baseline_trade_value == 100.0
    assert result.ratio == 3.0


def test_insufficient_history_returns_none_ratio_with_reason():
    result = compute_trade_value_surge_ratio(
        current_trade_value=Decimal("1000"),
        historical_trade_values=[Decimal("100"), None, None],
        min_lookback_days=3,
    )

    assert result.ratio is None
    assert result.reason_code == "insufficient_history"
    assert result.lookback_days_used == 1


def test_missing_current_trade_value_returns_none_with_reason_and_skips_history_check():
    result = compute_trade_value_surge_ratio(
        current_trade_value=None,
        historical_trade_values=[],
    )

    assert result.ratio is None
    assert result.reason_code == "missing_current_trade_value"
    assert result.lookback_days_used == 0
    assert result.baseline_trade_value is None


def test_zero_baseline_returns_none_with_reason_instead_of_dividing_by_zero():
    result = compute_trade_value_surge_ratio(
        current_trade_value=Decimal("500"),
        historical_trade_values=[Decimal("0"), Decimal("0"), Decimal("0")],
    )

    assert result.ratio is None
    assert result.reason_code == "zero_baseline_trade_value"
    assert result.lookback_days_used == 3
