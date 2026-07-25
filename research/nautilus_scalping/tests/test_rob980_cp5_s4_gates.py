from __future__ import annotations

import dataclasses
import math

import pytest
import rob974_h3_s4 as s4
from rob974_h3_manifest import FROZEN_S4_CONFIGS, PAIRS, get_config


def test_cp5_gate_behavior_exists_on_the_importable_cp4_estimator():
    assert hasattr(s4, "evaluate_s4_gates"), (
        "ROB-980 CP5 S4 gate behavior is not implemented"
    )


def _estimate(config_id: str = "S4-00", **changes):
    pair = changes.pop("pair", "XRP-DOGE")
    symbols = {
        "XRP-DOGE": ("XRPUSDT", "DOGEUSDT"),
        "XRP-SOL": ("XRPUSDT", "SOLUSDT"),
        "DOGE-SOL": ("DOGEUSDT", "SOLUSDT"),
    }[pair]
    values = {
        "config_id": config_id,
        "decision_ts": 144_000_000,
        "pair": pair,
        "symbol_a": symbols[0],
        "symbol_b": symbols[1],
        "beta_a": 1.0,
        "beta_b": 1.0,
        "beta_a_first": 1.0,
        "beta_a_second": 1.0,
        "beta_b_first": 1.0,
        "beta_b_second": 1.0,
        "weight_a": 0.5,
        "weight_b": 0.5,
        "spread": 0.018,
        "mu": 0.0,
        "mad": 0.01,
        "effective_mad_scale": 0.014826,
        "z": 1.8,
        "prior_beta_a": 1.0,
        "prior_beta_b": 1.0,
        "prior_weight_a": 0.5,
        "prior_weight_b": 0.5,
        "prior_mu": 0.0,
        "prior_mad": 0.01,
        "prior_effective_mad_scale": 0.014826,
        "z_prior": 2.0,
        "D_fraction": 0.018,
        "D_bps": 180.0,
        "rho": 0.60,
        "phi": 0.75,
        "half_life_4h_bars": 2.0,
        "beta_stability": 0.20,
        "sigma_pair": 0.0,
        "pair_return_fraction": -0.001,
        "pair_return_bps": -10.0,
        "current_market_return_4h": 0.50,
    }
    values.update(changes)
    return s4.S4Estimate(**values)


def _reason(estimate, config_id: str = "S4-00"):
    return s4.evaluate_s4_gates(estimate, get_config(config_id)).no_signal_reason


def test_convergence_exact_equality_and_both_entry_thresholds_pass():
    outcome = s4.evaluate_s4_gates(_estimate(), get_config("S4-00"))
    assert outcome.no_signal_reason is None
    assert outcome.candidate is not None
    assert outcome.candidate.side == "short_a_long_b"


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"z": 0.0}, "convergence_sign"),
        ({"z_prior": -2.0}, "convergence_sign"),
        ({"z_prior": math.nextafter(1.8, -math.inf)}, "prior_z_entry"),
        ({"z": math.nextafter(1.8, -math.inf)}, "current_z_entry"),
        ({"z": math.nextafter(1.8, math.inf)}, "convergence_fraction"),
        ({"rho": math.nextafter(0.60, -math.inf)}, "rho"),
        ({"half_life_4h_bars": math.nextafter(2.0, -math.inf)}, "half_life"),
        ({"half_life_4h_bars": math.nextafter(12.0, math.inf)}, "half_life"),
        ({"beta_stability": math.nextafter(0.20, math.inf)}, "beta_stability"),
        ({"D_bps": math.nextafter(180.0, -math.inf)}, "absolute_distance"),
    ),
)
def test_each_convergence_and_quality_boundary_one_ulp_outside_fails(changes, reason):
    assert _reason(_estimate(**changes)) == reason


def test_inclusive_half_life_rho_beta_stability_and_d_min_upper_boundaries_pass():
    assert _reason(_estimate(half_life_4h_bars=12.0)) is None
    assert _reason(_estimate(rho=0.60, beta_stability=0.20, D_bps=180.0)) is None
    prior_threshold = _estimate(z_prior=1.8, z=1.8)
    assert _reason(prior_threshold) == "convergence_fraction"


def test_distance_to_tp_equality_passes_and_one_ulp_outside_fails():
    config = get_config("S4-09")
    _, d_tp = s4.s4_risk_distances(config, 0.0)
    exact = _estimate("S4-09", D_fraction=1.25 * d_tp, D_bps=1.25 * d_tp * 10_000.0)
    assert _reason(exact, "S4-09") is None
    outside = dataclasses.replace(
        exact,
        D_fraction=math.nextafter(exact.D_fraction, -math.inf),
        D_bps=math.nextafter(exact.D_fraction, -math.inf) * 10_000.0,
    )
    assert _reason(outside, "S4-09") == "distance_to_tp"


def test_exact_risk_clips_and_all_registered_tp_floors():
    baseline = get_config("S4-00")
    assert s4.s4_risk_distances(baseline, 0.0) == (0.008, 0.012)
    assert s4.s4_risk_distances(baseline, 0.01) == (0.0125, 0.018750000000000003)
    assert s4.s4_risk_distances(baseline, 1.0) == (0.016, 0.024)
    floors = tuple(s4.s4_risk_distances(config, 0.0)[1] for config in FROZEN_S4_CONFIGS)
    assert min(floors) == 0.0108
    assert floors[0] == 0.012


def test_gross_notional_feasibility_is_inclusive_and_uses_exact_g_min():
    balanced = s4.historical_notional(0.5, 0.5)
    assert (balanced.G_min, balanced.G_max, balanced.G) == (12.0, 20.0, 12.0)
    boundary = s4.historical_notional(0.375, 0.625)
    assert boundary.G_min == boundary.G_max == boundary.G == 16.0
    outside = s4.historical_notional(math.nextafter(0.375, -math.inf), 0.625)
    assert outside.G_min > outside.G_max
    assert outside.G is None


def test_exact_direction_frozen_weights_null_executor_and_volatility_provenance():
    positive = s4.evaluate_s4_gates(_estimate(), get_config("S4-00")).candidate
    negative = s4.evaluate_s4_gates(
        _estimate(z=-1.8, z_prior=-2.0), get_config("S4-00")
    ).candidate
    assert positive is not None and negative is not None
    assert (positive.side_a, positive.side_b) == ("short", "long")
    assert (negative.side_a, negative.side_b) == ("long", "short")
    assert positive.weight_a == positive.weight_b == 0.5
    assert positive.gross_notional_usd == 12.0
    assert positive.notional_a_usd == positive.notional_b_usd == 6.0
    assert (
        positive.historical_notional_assumption
        == "frozen_continuous_6_to_10_usd_per_leg"
    )
    assert positive.historical_eligibility is True
    assert positive.volatility_percentile is None
    assert positive.volatility_percentile_provenance == "not_defined_for_s4"
    assert positive.leg_a_order_id is None
    assert positive.leg_b_order_id is None
    assert positive.leg_a_fill_id is None
    assert positive.leg_b_fill_id is None
    assert positive.pair_executor_provenance == "not_evaluated_h3_generator"
    assert positive.entry_tick_ts == positive.decision_ts
    assert positive.entry_deadline_ts == positive.decision_ts + 60_000
    assert positive.max_hold_4h_bars == 9


def _candidate(pair: str, *, distance: float, z_value: float, rho: float):
    estimate = _estimate(
        pair=pair,
        z=z_value,
        z_prior=math.copysign(max(abs(z_value) / 0.90, 2.0), z_value),
        D_fraction=distance,
        D_bps=distance * 10_000.0,
        rho=rho,
    )
    outcome = s4.evaluate_s4_gates(estimate, get_config("S4-00"))
    assert outcome.candidate is not None
    return outcome.candidate


@pytest.mark.parametrize(
    ("specs", "winner"),
    (
        (
            (("XRP-DOGE", 0.020, 2.0, 0.70), ("XRP-SOL", 0.021, 1.8, 0.60)),
            "XRP-SOL",
        ),
        (
            (("XRP-DOGE", 0.020, 1.9, 0.70), ("XRP-SOL", 0.020, 2.0, 0.60)),
            "XRP-SOL",
        ),
        (
            (("XRP-DOGE", 0.020, 2.0, 0.70), ("XRP-SOL", 0.020, 2.0, 0.80)),
            "XRP-SOL",
        ),
        (tuple((pair, 0.020, 2.0, 0.70) for pair in reversed(PAIRS)), "DOGE-SOL"),
    ),
)
def test_global_pair_arbitration_exact_rank_tiers(specs, winner):
    candidates = tuple(
        _candidate(pair, distance=distance, z_value=z_value, rho=rho)
        for pair, distance, z_value, rho in specs
    )
    result = s4.arbitrate_s4_candidates(candidates)
    assert result.winner.pair == winner
    assert len(result.rejected) == len(candidates) - 1
    assert all(
        item.reason == "simultaneous_pair_arbitration_loser" for item in result.rejected
    )
    assert result.winner.identity not in {
        item.candidate.identity for item in result.rejected
    }
