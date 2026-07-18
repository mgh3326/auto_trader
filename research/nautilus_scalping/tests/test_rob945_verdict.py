"""ROB-945 (H5) -- verdict authority RED tests.

Allowed verdicts: ``historical_pass | historical_fail | incomplete``;
readiness is always ``historical_screen_only``. ``historical_pass`` requires
ALL FIVE frozen criteria (boundary values PASS for expectancy/PF/folds/
concentration; @22 expectancy must be STRICTLY > 0). Structural/accounting/
evidence gaps are ``incomplete``, never ``historical_fail``.
"""

from __future__ import annotations

from rob945_scenario_metrics import StrategyScenarioAggregate
from rob945_verdict import (
    HISTORICAL_FAIL,
    HISTORICAL_PASS,
    INCOMPLETE,
    READINESS,
    evaluate_historical_verdict,
)


def _aggregate(
    *,
    scenario_name="primary_stress",
    net_expectancy_bps=10.0,
    profit_factor=2.0,
    monthly_concentration=0.3,
    monthly_concentration_reason=None,
    incomplete=False,
    incomplete_reason=None,
):
    return StrategyScenarioAggregate(
        strategy="S1",
        scenario_name=scenario_name,
        trade_count=10,
        net_expectancy_bps=net_expectancy_bps,
        pooled_expectancy_bps=net_expectancy_bps,
        profit_factor=profit_factor,
        win_rate=0.6,
        net_pnl_bps=100.0,
        timeout_ratio=0.1,
        mdd_r=1.0,
        mdd_reason=None,
        monthly_concentration=monthly_concentration,
        monthly_concentration_reason=monthly_concentration_reason,
        symbol_metrics=(),
        incomplete=incomplete,
        incomplete_reason=incomplete_reason,
    )


def _evaluate(**overrides):
    defaults = {
        "primary_stress": _aggregate(),
        "upward_stress": _aggregate(
            scenario_name="upward_stress", net_expectancy_bps=5.0
        ),
        "positive_oos_fold_count": 4,
        "accounting_complete": True,
        "accounting_incomplete_reason": None,
    }
    defaults.update(overrides)
    return evaluate_historical_verdict(**defaults)


def test_all_criteria_satisfied_is_historical_pass_with_no_reasons():
    result = _evaluate()
    assert result.verdict == HISTORICAL_PASS
    assert result.readiness == READINESS
    assert result.reason_codes == ()


def test_expectancy_exactly_at_five_bp_boundary_passes():
    result = _evaluate(primary_stress=_aggregate(net_expectancy_bps=5.0))
    assert result.verdict == HISTORICAL_PASS


def test_expectancy_just_below_five_bp_fails():
    result = _evaluate(primary_stress=_aggregate(net_expectancy_bps=4.99))
    assert result.verdict == HISTORICAL_FAIL
    assert "expectancy_below_5bp_threshold" in result.reason_codes


def test_profit_factor_exactly_at_1_15_boundary_passes():
    result = _evaluate(primary_stress=_aggregate(profit_factor=1.15))
    assert result.verdict == HISTORICAL_PASS


def test_profit_factor_just_below_1_15_fails():
    result = _evaluate(primary_stress=_aggregate(profit_factor=1.1499999))
    assert result.verdict == HISTORICAL_FAIL
    assert "profit_factor_below_1_15_threshold" in result.reason_codes


def test_exactly_four_positive_folds_passes():
    result = _evaluate(positive_oos_fold_count=4)
    assert result.verdict == HISTORICAL_PASS


def test_three_positive_folds_fails():
    result = _evaluate(positive_oos_fold_count=3)
    assert result.verdict == HISTORICAL_FAIL
    assert "insufficient_positive_oos_folds" in result.reason_codes


def test_concentration_exactly_at_50_percent_boundary_passes():
    result = _evaluate(primary_stress=_aggregate(monthly_concentration=0.5))
    assert result.verdict == HISTORICAL_PASS


def test_concentration_just_above_50_percent_fails():
    result = _evaluate(primary_stress=_aggregate(monthly_concentration=0.500001))
    assert result.verdict == HISTORICAL_FAIL
    assert "monthly_concentration_above_50_percent" in result.reason_codes


def test_no_positive_months_is_a_fail_reason_not_incomplete():
    result = _evaluate(
        primary_stress=_aggregate(
            monthly_concentration=None,
            monthly_concentration_reason="no_positive_months",
        )
    )
    assert result.verdict == HISTORICAL_FAIL
    assert "no_positive_months" in result.reason_codes


def test_upward_stress_expectancy_strictly_zero_fails():
    result = _evaluate(
        upward_stress=_aggregate(scenario_name="upward_stress", net_expectancy_bps=0.0)
    )
    assert result.verdict == HISTORICAL_FAIL
    assert "upward_stress_expectancy_not_positive" in result.reason_codes


def test_upward_stress_expectancy_tiny_positive_passes():
    result = _evaluate(
        upward_stress=_aggregate(
            scenario_name="upward_stress", net_expectancy_bps=0.0001
        )
    )
    assert result.verdict == HISTORICAL_PASS


def test_primary_stress_incomplete_symbol_evidence_yields_incomplete_not_fail():
    result = _evaluate(
        primary_stress=_aggregate(
            incomplete=True, incomplete_reason="insufficient_oos_symbol_evidence"
        )
    )
    assert result.verdict == INCOMPLETE
    assert "insufficient_oos_symbol_evidence" in result.reason_codes


def test_accounting_incomplete_yields_incomplete_even_if_metrics_pass():
    result = _evaluate(
        accounting_complete=False,
        accounting_incomplete_reason="h6_accounting_incomplete",
    )
    assert result.verdict == INCOMPLETE
    assert "h6_accounting_incomplete" in result.reason_codes


def test_multiple_failures_are_sorted_and_deduplicated():
    result = _evaluate(
        primary_stress=_aggregate(net_expectancy_bps=1.0, profit_factor=1.0),
        positive_oos_fold_count=1,
    )
    assert result.verdict == HISTORICAL_FAIL
    assert result.reason_codes == tuple(sorted(set(result.reason_codes)))
    assert len(result.reason_codes) == 3
