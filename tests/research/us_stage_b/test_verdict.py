from __future__ import annotations

from datetime import date, timedelta

from research.us_stage_b.engine import CohortComparison, TradeOutcome
from research.us_stage_b.registry import US_CANDIDATE_ORDER
from research.us_stage_b.verdict import evaluate_falsification

from .conftest import candidate


def _outcome(binding, *, symbol: str, entry_session: date) -> TradeOutcome:
    return TradeOutcome(
        strategy_id=binding.strategy_id,
        contract_hash=binding.contract_hash,
        labels=binding.labels,
        symbol=symbol,
        signal_session=entry_session - timedelta(days=1),
        entry_session=entry_session,
        exit_session=entry_session + timedelta(days=1),
        status="completed",
        selection_rank=1,
        fixed_notional_usd=500.0,
        adv20_pre_proxy=5_000_001.0,
        tie_break_sha256=("00" * 31) + f"{int(symbol[1:]) % 256:02x}",
        entry_open=100.0,
        exit_adjusted_close=101.0,
        gross_return=0.01,
        base_net_return=0.008,
        sensitivity_net_return=0.009,
        volume_ratio20=2.0,
    )


def _cohort(
    binding,
    *,
    symbol: str,
    entry_session: date,
    base_excess: float,
    sensitivity_excess: float,
    volume_ratio20: float,
) -> CohortComparison:
    return CohortComparison(
        strategy_id=binding.strategy_id,
        contract_hash=binding.contract_hash,
        labels=binding.labels,
        symbol=symbol,
        signal_session=entry_session - timedelta(days=1),
        entry_session=entry_session,
        exit_session=entry_session + timedelta(days=1),
        liquidity_decile=9,
        eligible_universe_size=2,
        leave_one_out_member_count=1,
        excluded_entry_no_fill_count=0,
        excluded_maturity_close_count=0,
        status="completed",
        candidate_base_net_return=0.008,
        candidate_sensitivity_net_return=0.009,
        baseline_base_net_return=0.008 - base_excess,
        baseline_sensitivity_net_return=0.009 - sensitivity_excess,
        base_excess_return=base_excess,
        sensitivity_excess_return=sensitivity_excess,
        volume_ratio20=volume_ratio20,
        tie_break_sha256=("00" * 31) + f"{int(symbol[1:]) % 256:02x}",
    )


def _sample(
    binding,
    *,
    total_count: int,
    validation_2023_count: int,
    validation_2024_count: int,
    base_excess: float,
    sensitivity_excess: float,
    top_volume_excess: float | None = None,
) -> tuple[tuple[TradeOutcome, ...], tuple[CohortComparison, ...]]:
    dates = (
        tuple(
            date(2023, 1, 1) + timedelta(days=index)
            for index in range(validation_2023_count)
        )
        + tuple(
            date(2024, 1, 1) + timedelta(days=index)
            for index in range(validation_2024_count)
        )
        + tuple(
            date(2020, 1, 1) + timedelta(days=index)
            for index in range(
                total_count - validation_2023_count - validation_2024_count
            )
        )
    )
    outcomes = tuple(
        _outcome(binding, symbol=f"S{index:05d}", entry_session=entry_session)
        for index, entry_session in enumerate(dates)
    )
    validation_count = validation_2023_count + validation_2024_count
    cohorts = tuple(
        _cohort(
            binding,
            symbol=f"S{index:05d}",
            entry_session=dates[index],
            base_excess=(
                top_volume_excess
                if index == 0 and top_volume_excess is not None
                else base_excess
            ),
            sensitivity_excess=sensitivity_excess,
            volume_ratio20=100.0 if index == 0 else 2.0,
        )
        for index in range(validation_count)
    )
    return outcomes, cohorts


def test_mom_rejects_nonpositive_validation_session_weighted_cohort_excess(
    registry,
) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[0])
    outcomes, cohorts = _sample(
        binding,
        total_count=400,
        validation_2023_count=30,
        validation_2024_count=30,
        base_excess=-0.001,
        sensitivity_excess=-0.001,
    )
    verdict = evaluate_falsification(
        candidate=binding,
        outcomes=outcomes,
        cohorts=cohorts,
        run_invalid=False,
        invalid_reasons=(),
    )

    assert verdict.state == "FALSIFIED_VALIDATION_COHORT_EXCESS"
    assert (
        verdict.gate_results[
            "validation_entry_session_equal_weighted_base_excess_gt_zero"
        ]
        is False
    )


def test_rev_identifies_the_frozen_10bp_fail_5bp_positive_cost_sensitive_case(
    registry,
) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[1])
    outcomes, cohorts = _sample(
        binding,
        total_count=1_000,
        validation_2023_count=100,
        validation_2024_count=100,
        base_excess=-0.001,
        sensitivity_excess=0.001,
    )
    verdict = evaluate_falsification(
        candidate=binding,
        outcomes=outcomes,
        cohorts=cohorts,
        run_invalid=False,
        invalid_reasons=(),
    )

    assert verdict.state == "FALSIFIED_COST_SENSITIVE"
    assert verdict.gate_results["cost_sensitive_failure"] is True


def test_volbreak_rejects_top_one_percent_extreme_dependence(registry) -> None:
    binding = candidate(registry, US_CANDIDATE_ORDER[2])
    outcomes, cohorts = _sample(
        binding,
        total_count=500,
        validation_2023_count=50,
        validation_2024_count=50,
        base_excess=0.001,
        sensitivity_excess=0.001,
        top_volume_excess=0.2,
    )
    verdict = evaluate_falsification(
        candidate=binding,
        outcomes=outcomes,
        cohorts=cohorts,
        run_invalid=False,
        invalid_reasons=(),
    )

    assert verdict.state == "FALSIFIED_EXTREME_DEPENDENCE"
    assert verdict.gate_results["top_1_percent_count"] == 1
    assert verdict.gate_results["top_1_percent_excess_contribution_ratio"] > 0.5
