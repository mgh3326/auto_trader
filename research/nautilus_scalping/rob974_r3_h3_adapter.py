"""Additive R3 gates over frozen R2 H3 DTOs and shared atomic predicates.

The R2 H3 modules are members of the frozen R2 production source inventory,
so this adapter deliberately leaves their bytes untouched.  It reuses their
validated metric/estimate and candidate/outcome DTOs while applying only the
four preregistered R3 threshold axes.
"""

from __future__ import annotations

import rob974_h3_gate_predicates as predicates
import rob974_h3_h2_adapter as h3_h2_adapter
import rob974_h3_s3 as s3
import rob974_h3_s4 as s4
from rob974_features import MINUTE_MS
from rob974_h2_dtos import Z_ENTRY_ABS_MIN, S4PairSignalIntent
from rob974_r3_manifest import (
    R3S3Config,
    R3S4Config,
    assert_registered_r3_config,
    get_r3_config,
)

__all__ = [
    "R3H2ExecutionSeamBlocked",
    "adapt_r3_s4_candidate_for_h2",
    "evaluate_r3_s3_gates",
    "evaluate_r3_s4_gates",
]


class R3H2ExecutionSeamBlocked(RuntimeError):
    """An accepted R3 candidate is outside the frozen R2 H2 DTO contract."""


def _s3_reject(reason: str, side: str | None = None) -> s3.S3GateOutcome:
    return s3.S3GateOutcome(side, None, reason)


def _s3_risk_distances(config: R3S3Config, a_value: float) -> tuple[float, float]:
    if type(a_value) is not float:
        raise TypeError("A must be built-in float")
    if a_value < 0.0:
        raise ValueError("A must not be negative")
    d_sl = min(max(config.k_SL * a_value, 0.008), 0.020)
    d_tp = max(0.0068, config.R_TP * d_sl)
    return d_sl, d_tp


def evaluate_r3_s3_gates(metrics: s3.S3Metrics, config: R3S3Config) -> s3.S3GateOutcome:
    """Compose shared atomics in the inherited S3 first-fail order."""

    if type(metrics) is not s3.S3Metrics:
        raise TypeError("R3 S3 gates require exact S3Metrics")
    if type(config) is not R3S3Config:
        raise TypeError("R3 S3 gates require exact R3S3Config")
    assert_registered_r3_config(config)
    if metrics.config_id != config.config_id:
        raise ValueError("metrics/config identity mismatch")

    atomic = predicates.evaluate_s3_threshold_predicates(
        market_return_24h=metrics.market_return_24h,
        bplus=metrics.bplus,
        bminus=metrics.bminus,
        trend_strength=metrics.S,
        s_min=config.S_min,
        m_min_bp=config.M_min_bp,
    )
    if not atomic.market_magnitude:
        return _s3_reject("market_regime")
    side = atomic.market_direction
    if not atomic.market_breadth:
        return _s3_reject("market_breadth", side)
    if not atomic.trend_sign or not atomic.trend_magnitude:
        return _s3_reject("trend_strength", side)
    if metrics.ER < config.ER_min:
        return _s3_reject("efficiency", side)
    pullback = metrics.Qplus if side == "long" else metrics.Qminus
    if not config.q_min <= pullback <= 1.25:
        return _s3_reject("pullback_depth", side)
    if (side == "long" and metrics.close <= metrics.vwap12) or (
        side == "short" and metrics.close >= metrics.vwap12
    ):
        return _s3_reject("vwap_reclaim", side)
    if (side == "long" and metrics.close <= metrics.previous_close) or (
        side == "short" and metrics.close >= metrics.previous_close
    ):
        return _s3_reject("momentum", side)
    if (side == "long" and metrics.close >= metrics.prior_l_high) or (
        side == "short" and metrics.close <= metrics.prior_l_low
    ):
        return _s3_reject("prior_l_non_breakout", side)
    if not 20.0 <= metrics.percentile_30d <= 90.0:
        return _s3_reject("volatility_percentile", side)
    d_sl, d_tp = _s3_risk_distances(config, metrics.A)
    if d_tp > 0.60 * metrics.range24:
        return _s3_reject("range_tp_capacity", side)

    return s3.S3GateOutcome(
        side,
        s3.S3Candidate(
            "S3",
            config.config_id,
            metrics.decision_ts,
            metrics.symbol,
            side,
            metrics.R,
            metrics.S,
            metrics.ER,
            pullback,
            metrics.A,
            metrics.atr20,
            metrics.close,
            metrics.vwap12,
            metrics.vwap24,
            metrics.market_return_24h,
            metrics.current_market_return_4h,
            metrics.percentile_30d,
            "h1_percentile_30d",
            metrics.range24,
            d_sl,
            d_tp,
            metrics.decision_ts,
            metrics.decision_ts + MINUTE_MS,
            12,
        ),
        None,
    )


def _s4_reject(reason: str, side: str | None = None) -> s4.S4GateOutcome:
    return s4.S4GateOutcome(side, None, reason)


def _s4_risk_distances(config: R3S4Config, sigma_pair: float) -> tuple[float, float]:
    if type(sigma_pair) is not float:
        raise TypeError("sigma_pair must be built-in float")
    if sigma_pair < 0.0:
        raise ValueError("sigma_pair must not be negative")
    d_sl = min(max(config.k_SL * sigma_pair, 0.008), 0.016)
    d_tp = max(0.0068, config.R_TP * d_sl)
    return d_sl, d_tp


def evaluate_r3_s4_gates(
    estimate: s4.S4Estimate, config: R3S4Config
) -> s4.S4GateOutcome:
    """Compose shared atomics in the inherited S4 first-fail order."""

    if type(estimate) is not s4.S4Estimate:
        raise TypeError("R3 S4 gates require exact S4Estimate")
    if type(config) is not R3S4Config:
        raise TypeError("R3 S4 gates require exact R3S4Config")
    assert_registered_r3_config(config)
    if estimate.config_id != config.config_id:
        raise ValueError("estimate/config identity mismatch")

    if not predicates.s4_convergence_sign_passes(estimate.z_prior, estimate.z):
        return _s4_reject("convergence_sign")
    side = "short_a_long_b" if estimate.z > 0.0 else "long_a_short_b"
    if not predicates.s4_prior_z_magnitude_passes(estimate.z_prior, config.z_entry):
        return _s4_reject("prior_z_entry", side)
    if not predicates.s4_current_z_magnitude_passes(estimate.z, config.z_entry):
        return _s4_reject("current_z_entry", side)
    if abs(estimate.z) > 0.90 * abs(estimate.z_prior):
        return _s4_reject("convergence_fraction", side)
    if estimate.rho < 0.60:
        return _s4_reject("rho", side)
    if not 2.0 <= estimate.half_life_4h_bars <= 12.0:
        return _s4_reject("half_life", side)
    if estimate.beta_stability > 0.20:
        return _s4_reject("beta_stability", side)
    if not predicates.s4_absolute_distance_passes(estimate.D_bps, config.d_min_bp):
        return _s4_reject("absolute_distance", side)
    d_sl, d_tp = _s4_risk_distances(config, estimate.sigma_pair)
    if not predicates.s4_distance_to_tp_passes(estimate.D_fraction, d_tp):
        return _s4_reject("distance_to_tp", side)
    sizing = s4.historical_notional(estimate.weight_a, estimate.weight_b)
    if sizing.G is None:
        return _s4_reject("historical_notional_feasibility", side)

    side_a, side_b = (
        ("short", "long") if side == "short_a_long_b" else ("long", "short")
    )
    return s4.S4GateOutcome(
        side,
        s4.S4Candidate(
            "S4",
            config.config_id,
            estimate.decision_ts,
            estimate.pair,
            side,
            estimate.symbol_a,
            estimate.symbol_b,
            side_a,
            side_b,
            estimate.beta_a,
            estimate.beta_b,
            estimate.weight_a,
            estimate.weight_b,
            estimate.mu,
            estimate.mad,
            estimate.effective_mad_scale,
            estimate.z,
            estimate.z_prior,
            estimate.D_fraction,
            estimate.D_bps,
            estimate.rho,
            estimate.half_life_4h_bars,
            estimate.beta_stability,
            estimate.sigma_pair,
            estimate.pair_return_fraction,
            sizing.G,
            estimate.weight_a * sizing.G,
            estimate.weight_b * sizing.G,
            d_sl,
            d_tp,
            s4.HISTORICAL_NOTIONAL_ASSUMPTION,
            True,
            "rob974_h1_parent_manifest_selected_universe",
            None,
            "not_defined_for_s4",
            estimate.decision_ts,
            estimate.decision_ts + MINUTE_MS,
            9,
            None,
            None,
            None,
            None,
            "not_evaluated_h3_generator",
        ),
        None,
    )


def adapt_r3_s4_candidate_for_h2(
    candidate: s4.S4Candidate, *, fold_id: str
) -> S4PairSignalIntent:
    """Delegate only candidates compatible with the frozen R2 H2 DTO.

    R3 deliberately preregisters S4 thresholds below H2's historical
    ``|z_entry| >= 1`` DTO floor.  The observed z is never clamped or replaced
    with the unsigned threshold: incompatible candidates stop at this additive
    execution seam before the frozen adapter is called.
    """

    if type(candidate) is not s4.S4Candidate:
        raise TypeError("candidate must be exact H3 S4Candidate")
    config = get_r3_config(candidate.config_id)
    if type(config) is not R3S4Config:
        raise R3H2ExecutionSeamBlocked(
            "R3 S4 execution seam requires a frozen R3 S4 config"
        )
    if abs(candidate.observed_z) < Z_ENTRY_ABS_MIN:
        raise R3H2ExecutionSeamBlocked(
            "R3 observed |z| is below the frozen H2 S4PairSignalIntent floor; "
            "candidate was not adapted"
        )
    return h3_h2_adapter.adapt_s4_candidate(candidate, fold_id=fold_id)
