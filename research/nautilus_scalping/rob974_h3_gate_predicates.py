"""Shared atomic threshold predicates for ROB-974 R2 and R3 H3 gates.

These helpers own only mechanical inclusive comparisons.  They do not own a
roster, candidate construction, gate ordering, or empirical observations, so
R2 generation and R3 diagnostics can consume the same boolean authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "S3ThresholdPredicates",
    "S4ThresholdPredicates",
    "evaluate_s3_threshold_predicates",
    "evaluate_s4_threshold_predicates",
    "s3_market_breadth_passes",
    "s3_market_direction",
    "s3_market_magnitude_passes",
    "s3_trend_magnitude_passes",
    "s3_trend_sign_passes",
    "s4_absolute_distance_passes",
    "s4_convergence_sign_passes",
    "s4_current_z_magnitude_passes",
    "s4_distance_to_tp_passes",
    "s4_prior_z_magnitude_passes",
]

S3Side = str
S4Side = str


def _float(value: object, name: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite built-in float")
    return value


def _nonnegative_float(value: object, name: str) -> float:
    result = _float(value, name)
    if result < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative built-in int")
    return value


def _s3_side(value: object) -> str:
    if type(value) is not str or value not in ("long", "short"):
        raise ValueError("S3 side must be long or short")
    return value


def s3_market_magnitude_passes(market_return_24h: float, m_min_bp: int) -> bool:
    value = _float(market_return_24h, "market_return_24h")
    threshold = _nonnegative_int(m_min_bp, "m_min_bp") / 10_000.0
    return abs(value) >= threshold


def s3_market_direction(market_return_24h: float) -> S3Side:
    """Return sign direction independently of magnitude; zero is long-first."""

    value = _float(market_return_24h, "market_return_24h")
    return "long" if value >= 0.0 else "short"


def s3_market_breadth_passes(
    side: S3Side, bplus: int, bminus: int, breadth_min: int = 2
) -> bool:
    direction = _s3_side(side)
    plus = _nonnegative_int(bplus, "bplus")
    minus = _nonnegative_int(bminus, "bminus")
    threshold = _nonnegative_int(breadth_min, "breadth_min")
    return (plus if direction == "long" else minus) >= threshold


def s3_trend_sign_passes(side: S3Side, trend_strength: float) -> bool:
    direction = _s3_side(side)
    value = _float(trend_strength, "trend_strength")
    return value >= 0.0 if direction == "long" else value <= 0.0


def s3_trend_magnitude_passes(trend_strength: float, s_min: float) -> bool:
    value = _float(trend_strength, "trend_strength")
    threshold = _nonnegative_float(s_min, "s_min")
    return abs(value) >= threshold


@dataclass(frozen=True, slots=True)
class S3ThresholdPredicates:
    market_direction: S3Side
    market_magnitude: bool
    market_breadth: bool
    trend_sign: bool
    trend_magnitude: bool


def evaluate_s3_threshold_predicates(
    *,
    market_return_24h: float,
    bplus: int,
    bminus: int,
    trend_strength: float,
    s_min: float,
    m_min_bp: int,
    breadth_min: int = 2,
) -> S3ThresholdPredicates:
    direction = s3_market_direction(market_return_24h)
    return S3ThresholdPredicates(
        market_direction=direction,
        market_magnitude=s3_market_magnitude_passes(market_return_24h, m_min_bp),
        market_breadth=s3_market_breadth_passes(direction, bplus, bminus, breadth_min),
        trend_sign=s3_trend_sign_passes(direction, trend_strength),
        trend_magnitude=s3_trend_magnitude_passes(trend_strength, s_min),
    )


def s4_convergence_sign_passes(z_prior: float, z_current: float) -> bool:
    prior = _float(z_prior, "z_prior")
    current = _float(z_current, "z_current")
    return (
        prior != 0.0
        and current != 0.0
        and math.copysign(1.0, prior) == math.copysign(1.0, current)
    )


def s4_prior_z_magnitude_passes(z_prior: float, z_entry: float) -> bool:
    prior = _float(z_prior, "z_prior")
    threshold = _nonnegative_float(z_entry, "z_entry")
    return abs(prior) >= threshold


def s4_current_z_magnitude_passes(z_current: float, z_entry: float) -> bool:
    current = _float(z_current, "z_current")
    threshold = _nonnegative_float(z_entry, "z_entry")
    return abs(current) >= threshold


def s4_absolute_distance_passes(d_bps: float, d_min_bp: int) -> bool:
    distance = _nonnegative_float(d_bps, "D_bps")
    threshold = _nonnegative_int(d_min_bp, "d_min_bp")
    return distance >= float(threshold)


def s4_distance_to_tp_passes(d_fraction: float, d_tp: float) -> bool:
    distance = _nonnegative_float(d_fraction, "D_fraction")
    take_profit = _nonnegative_float(d_tp, "d_TP")
    return distance >= 1.25 * take_profit


@dataclass(frozen=True, slots=True)
class S4ThresholdPredicates:
    side: S4Side | None
    convergence_sign: bool
    prior_z_magnitude: bool
    current_z_magnitude: bool
    absolute_distance: bool
    distance_to_tp: bool


def evaluate_s4_threshold_predicates(
    *,
    z_prior: float,
    z_current: float,
    z_entry: float,
    d_bps: float,
    d_min_bp: int,
    d_fraction: float,
    d_tp: float,
) -> S4ThresholdPredicates:
    convergence = s4_convergence_sign_passes(z_prior, z_current)
    current = _float(z_current, "z_current")
    side = (
        ("short_a_long_b" if current > 0.0 else "long_a_short_b")
        if convergence
        else None
    )
    return S4ThresholdPredicates(
        side=side,
        convergence_sign=convergence,
        prior_z_magnitude=s4_prior_z_magnitude_passes(z_prior, z_entry),
        current_z_magnitude=s4_current_z_magnitude_passes(z_current, z_entry),
        absolute_distance=s4_absolute_distance_passes(d_bps, d_min_bp),
        distance_to_tp=s4_distance_to_tp_passes(d_fraction, d_tp),
    )
