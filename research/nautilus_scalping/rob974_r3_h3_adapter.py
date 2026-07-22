"""Additive R3 gates over frozen R2 H3 DTOs and shared atomic predicates.

The R2 H3 modules are members of the frozen R2 production source inventory,
so this adapter deliberately leaves their bytes untouched.  It reuses their
validated metric/estimate and candidate/outcome DTOs while applying only the
four preregistered R3 threshold axes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

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
    "R3S3GateAtoms",
    "R3S4GateAtoms",
    "R3S4GateObservation",
    "adapt_r3_s4_candidate_for_h2",
    "evaluate_r3_s3_atoms",
    "evaluate_r3_s3_gates",
    "evaluate_r3_s4_atoms",
    "evaluate_r3_s4_gates",
]


@dataclass(frozen=True, slots=True)
class R3S3GateAtoms:
    side: str
    market_magnitude: bool
    market_breadth: bool
    trend_sign_alignment: bool
    trend_magnitude: bool
    efficiency_ratio: bool
    pullback_depth: bool
    vwap_reclaim: bool
    momentum: bool
    prior_l_non_breakout: bool
    volatility_percentile: bool
    range_to_tp_capacity: bool

    @property
    def gate_results(self) -> tuple[tuple[str, bool], ...]:
        return tuple(
            (name, getattr(self, name))
            for name in (
                "market_magnitude",
                "market_breadth",
                "trend_sign_alignment",
                "trend_magnitude",
                "efficiency_ratio",
                "pullback_depth",
                "vwap_reclaim",
                "momentum",
                "prior_l_non_breakout",
                "volatility_percentile",
                "range_to_tp_capacity",
            )
        )


@dataclass(frozen=True, slots=True)
class R3S4GateObservation:
    """Context-valid S4 atomic inputs, including finite phi outside (0, 1)."""

    config_id: str
    decision_ts: int
    pair: str
    phi: float
    z_prior: float
    z_current: float
    rho: float
    half_life_4h_bars: float | None
    beta_stability: float
    d_bps: float
    d_fraction: float
    sigma_pair: float
    weight_a: float
    weight_b: float

    def __post_init__(self) -> None:
        if type(self.config_id) is not str or not self.config_id:
            raise TypeError("config_id must be a non-empty built-in str")
        if type(self.decision_ts) is not int:
            raise TypeError("decision_ts must be built-in int")
        if type(self.pair) is not str or self.pair not in s4.PAIR_ORDER:
            raise ValueError("pair outside frozen S4 order")
        for name in (
            "phi",
            "z_prior",
            "z_current",
            "rho",
            "beta_stability",
            "d_bps",
            "d_fraction",
            "sigma_pair",
            "weight_a",
            "weight_b",
        ):
            value = getattr(self, name)
            if type(value) is not float or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite built-in float")
        if 0.0 < self.phi < 1.0:
            if type(self.half_life_4h_bars) is not float or not math.isfinite(
                self.half_life_4h_bars
            ):
                raise ValueError("open-unit phi requires finite half-life")
        elif self.half_life_4h_bars is not None:
            raise ValueError("phi outside the open unit interval has no half-life")

    @classmethod
    def from_estimate(cls, estimate: s4.S4Estimate) -> R3S4GateObservation:
        if type(estimate) is not s4.S4Estimate:
            raise TypeError("estimate must be exact S4Estimate")
        return cls(
            config_id=estimate.config_id,
            decision_ts=estimate.decision_ts,
            pair=estimate.pair,
            phi=estimate.phi,
            z_prior=estimate.z_prior,
            z_current=estimate.z,
            rho=estimate.rho,
            half_life_4h_bars=estimate.half_life_4h_bars,
            beta_stability=estimate.beta_stability,
            d_bps=estimate.D_bps,
            d_fraction=estimate.D_fraction,
            sigma_pair=estimate.sigma_pair,
            weight_a=estimate.weight_a,
            weight_b=estimate.weight_b,
        )


@dataclass(frozen=True, slots=True)
class R3S4GateAtoms:
    side: str | None
    phi_open_unit_interval: bool
    convergence_sign: bool
    prior_z_magnitude: bool
    current_z_magnitude: bool
    convergence_fraction: bool
    rho: bool
    half_life: bool
    beta_stability: bool
    d_min_distance: bool
    distance_to_tp: bool
    notional_feasibility: bool

    @property
    def gate_results(self) -> tuple[tuple[str, bool], ...]:
        return tuple(
            (name, getattr(self, name))
            for name in (
                "phi_open_unit_interval",
                "convergence_sign",
                "prior_z_magnitude",
                "current_z_magnitude",
                "convergence_fraction",
                "rho",
                "half_life",
                "beta_stability",
                "d_min_distance",
                "distance_to_tp",
                "notional_feasibility",
            )
        )


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


def evaluate_r3_s3_atoms(metrics: s3.S3Metrics, config: R3S3Config) -> R3S3GateAtoms:
    """Evaluate every S3 gate without first-fail short-circuiting."""

    if type(metrics) is not s3.S3Metrics:
        raise TypeError("R3 S3 atoms require exact S3Metrics")
    if type(config) is not R3S3Config:
        raise TypeError("R3 S3 atoms require exact R3S3Config")
    assert_registered_r3_config(config)
    if metrics.config_id != config.config_id:
        raise ValueError("metrics/config identity mismatch")
    threshold = predicates.evaluate_s3_threshold_predicates(
        market_return_24h=metrics.market_return_24h,
        bplus=metrics.bplus,
        bminus=metrics.bminus,
        trend_strength=metrics.S,
        s_min=config.S_min,
        m_min_bp=config.M_min_bp,
    )
    side = threshold.market_direction
    pullback = metrics.Qplus if side == "long" else metrics.Qminus
    _d_sl, d_tp = _s3_risk_distances(config, metrics.A)
    return R3S3GateAtoms(
        side=side,
        market_magnitude=threshold.market_magnitude,
        market_breadth=threshold.market_breadth,
        trend_sign_alignment=threshold.trend_sign,
        trend_magnitude=threshold.trend_magnitude,
        efficiency_ratio=metrics.ER >= config.ER_min,
        pullback_depth=config.q_min <= pullback <= 1.25,
        vwap_reclaim=(
            metrics.close > metrics.vwap12
            if side == "long"
            else metrics.close < metrics.vwap12
        ),
        momentum=(
            metrics.close > metrics.previous_close
            if side == "long"
            else metrics.close < metrics.previous_close
        ),
        prior_l_non_breakout=(
            metrics.close < metrics.prior_l_high
            if side == "long"
            else metrics.close > metrics.prior_l_low
        ),
        volatility_percentile=20.0 <= metrics.percentile_30d <= 90.0,
        range_to_tp_capacity=d_tp <= 0.60 * metrics.range24,
    )


def evaluate_r3_s3_gates(metrics: s3.S3Metrics, config: R3S3Config) -> s3.S3GateOutcome:
    """Compose shared atomics in the inherited S3 first-fail order."""

    if type(metrics) is not s3.S3Metrics:
        raise TypeError("R3 S3 gates require exact S3Metrics")
    if type(config) is not R3S3Config:
        raise TypeError("R3 S3 gates require exact R3S3Config")
    assert_registered_r3_config(config)
    if metrics.config_id != config.config_id:
        raise ValueError("metrics/config identity mismatch")

    atomic = evaluate_r3_s3_atoms(metrics, config)
    if not atomic.market_magnitude:
        return _s3_reject("market_regime")
    side = atomic.side
    if not atomic.market_breadth:
        return _s3_reject("market_breadth", side)
    if not atomic.trend_sign_alignment or not atomic.trend_magnitude:
        return _s3_reject("trend_strength", side)
    if not atomic.efficiency_ratio:
        return _s3_reject("efficiency", side)
    pullback = metrics.Qplus if side == "long" else metrics.Qminus
    if not atomic.pullback_depth:
        return _s3_reject("pullback_depth", side)
    if not atomic.vwap_reclaim:
        return _s3_reject("vwap_reclaim", side)
    if not atomic.momentum:
        return _s3_reject("momentum", side)
    if not atomic.prior_l_non_breakout:
        return _s3_reject("prior_l_non_breakout", side)
    if not atomic.volatility_percentile:
        return _s3_reject("volatility_percentile", side)
    d_sl, d_tp = _s3_risk_distances(config, metrics.A)
    if not atomic.range_to_tp_capacity:
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


def evaluate_r3_s4_atoms(
    observation: R3S4GateObservation, config: R3S4Config
) -> R3S4GateAtoms:
    """Evaluate all eleven S4 atoms, including after a finite phi failure."""

    if type(observation) is not R3S4GateObservation:
        raise TypeError("R3 S4 atoms require exact R3S4GateObservation")
    if type(config) is not R3S4Config:
        raise TypeError("R3 S4 atoms require exact R3S4Config")
    assert_registered_r3_config(config)
    if observation.config_id != config.config_id:
        raise ValueError("observation/config identity mismatch")
    _d_sl, d_tp = _s4_risk_distances(config, observation.sigma_pair)
    convergence = predicates.s4_convergence_sign_passes(
        observation.z_prior, observation.z_current
    )
    side = None
    if observation.z_current != 0.0:
        side = "short_a_long_b" if observation.z_current > 0.0 else "long_a_short_b"
    phi_open = 0.0 < observation.phi < 1.0
    half_life = observation.half_life_4h_bars
    sizing = s4.historical_notional(observation.weight_a, observation.weight_b)
    return R3S4GateAtoms(
        side=side,
        phi_open_unit_interval=phi_open,
        convergence_sign=convergence,
        prior_z_magnitude=predicates.s4_prior_z_magnitude_passes(
            observation.z_prior, config.z_entry
        ),
        current_z_magnitude=predicates.s4_current_z_magnitude_passes(
            observation.z_current, config.z_entry
        ),
        convergence_fraction=(
            abs(observation.z_current) <= 0.90 * abs(observation.z_prior)
        ),
        rho=observation.rho >= 0.60,
        half_life=(phi_open and half_life is not None and 2.0 <= half_life <= 12.0),
        beta_stability=observation.beta_stability <= 0.20,
        d_min_distance=predicates.s4_absolute_distance_passes(
            observation.d_bps, config.d_min_bp
        ),
        distance_to_tp=predicates.s4_distance_to_tp_passes(
            observation.d_fraction, d_tp
        ),
        notional_feasibility=sizing.G is not None,
    )


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

    atomic = evaluate_r3_s4_atoms(R3S4GateObservation.from_estimate(estimate), config)
    if not atomic.phi_open_unit_interval:
        return _s4_reject("phi_not_in_open_unit_interval")
    if not atomic.convergence_sign:
        return _s4_reject("convergence_sign")
    side = atomic.side
    if side is None:
        raise AssertionError("convergence-valid observation must have a side")
    if not atomic.prior_z_magnitude:
        return _s4_reject("prior_z_entry", side)
    if not atomic.current_z_magnitude:
        return _s4_reject("current_z_entry", side)
    if not atomic.convergence_fraction:
        return _s4_reject("convergence_fraction", side)
    if not atomic.rho:
        return _s4_reject("rho", side)
    if not atomic.half_life:
        return _s4_reject("half_life", side)
    if not atomic.beta_stability:
        return _s4_reject("beta_stability", side)
    if not atomic.d_min_distance:
        return _s4_reject("absolute_distance", side)
    d_sl, d_tp = _s4_risk_distances(config, estimate.sigma_pair)
    if not atomic.distance_to_tp:
        return _s4_reject("distance_to_tp", side)
    sizing = s4.historical_notional(estimate.weight_a, estimate.weight_b)
    if not atomic.notional_feasibility:
        return _s4_reject("historical_notional_feasibility", side)
    if sizing.G is None:
        raise AssertionError("notional atom and sizing result drifted")

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
