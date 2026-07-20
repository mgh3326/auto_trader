"""ROB-974 H3 S4 exact point-in-time pair estimator.

Every evaluation uses its own W-wide beta-neutral weights over its historical
spread.  The stored prior z-score is a separate t-1 evaluation and therefore
cannot consume t betas, weights, closes, median, MAD, or scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rob974_features import FOUR_HOUR_MS, CommonSnapshot
from rob974_h3_manifest import PAIRS, SYMBOLS, S4Config, assert_registered_config
from rob974_h3_s3 import FeatureContext

PAIR_ORDER: tuple[str, ...] = PAIRS
_PAIR_SYMBOLS = {
    "XRP-DOGE": ("XRPUSDT", "DOGEUSDT"),
    "XRP-SOL": ("XRPUSDT", "SOLUSDT"),
    "DOGE-SOL": ("DOGEUSDT", "SOLUSDT"),
}
S4_ESTIMATION_REASONS: tuple[str, ...] = (
    "missing_required_context",
    "degenerate_beta_market_variance",
    "degenerate_rho_variance",
    "degenerate_phi_denominator",
    "phi_not_in_open_unit_interval",
    "nonfinite_required_input",
)


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    return value


def _float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _numeric_tuple(values: tuple[float, ...], name: str) -> bool:
    if type(values) is not tuple:
        raise TypeError(f"{name} must be built-in tuple")
    for value in values:
        if type(value) is not float:
            raise TypeError(f"{name} must contain built-in float")
    return bool(values) and all(math.isfinite(value) for value in values)


def _safe_fsum(values) -> float | None:
    materialized = tuple(values)
    if not all(type(value) is float and math.isfinite(value) for value in materialized):
        return None
    try:
        result = math.fsum(materialized)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def fixed_median(values: tuple[float, ...]) -> float | None:
    """Deterministic median; even middles are combined with fixed-order fsum."""
    if not _numeric_tuple(values, "values"):
        return None
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    combined = _safe_fsum((ordered[middle - 1], ordered[middle]))
    return None if combined is None else combined / 2.0


@dataclass(frozen=True, slots=True)
class SpreadStatistics:
    mu: float
    mad: float
    effective_scale: float

    def __post_init__(self) -> None:
        _float(self.mu, "mu")
        _float(self.mad, "mad")
        _float(self.effective_scale, "effective_scale")
        if self.mad < 0.0 or self.effective_scale < 1e-6:
            raise ValueError("invalid spread scale")


def spread_statistics(spreads: tuple[float, ...]) -> SpreadStatistics | None:
    if not _numeric_tuple(spreads, "spreads"):
        return None
    mu = fixed_median(spreads)
    if mu is None:
        return None
    deviations = tuple(abs(value - mu) for value in spreads)
    if not all(math.isfinite(value) for value in deviations):
        return None
    mad = fixed_median(deviations)
    if mad is None:
        return None
    scaled = 1.4826 * mad
    if not math.isfinite(scaled):
        return None
    return SpreadStatistics(mu, mad, max(scaled, 1e-6))


def _mean(values: tuple[float, ...]) -> float | None:
    total = _safe_fsum(values)
    return None if total is None else total / len(values)


def compute_clipped_beta(
    returns: tuple[float, ...], market: tuple[float, ...]
) -> float | None:
    if not _numeric_tuple(returns, "returns") or not _numeric_tuple(market, "market"):
        return None
    if len(returns) != len(market) or len(returns) < 2:
        return None
    return_mean = _mean(returns)
    market_mean = _mean(market)
    if return_mean is None or market_mean is None:
        return None
    covariance_terms = tuple(
        (return_value - return_mean) * (market_value - market_mean)
        for return_value, market_value in zip(returns, market, strict=True)
    )
    variance_terms = tuple((value - market_mean) ** 2 for value in market)
    numerator = _safe_fsum(covariance_terms)
    denominator = _safe_fsum(variance_terms)
    if numerator is None or denominator is None or denominator <= 0.0:
        return None
    beta = numerator / denominator
    if not math.isfinite(beta):
        return None
    return min(max(beta, 0.25), 3.0)


def correlation(left: tuple[float, ...], right: tuple[float, ...]) -> float | None:
    if not _numeric_tuple(left, "left") or not _numeric_tuple(right, "right"):
        return None
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    if left_mean is None or right_mean is None:
        return None
    left_centered = tuple(value - left_mean for value in left)
    right_centered = tuple(value - right_mean for value in right)
    covariance = _safe_fsum(
        tuple(
            a_value * b_value
            for a_value, b_value in zip(left_centered, right_centered, strict=True)
        )
    )
    left_ss = _safe_fsum(tuple(value * value for value in left_centered))
    right_ss = _safe_fsum(tuple(value * value for value in right_centered))
    if (
        covariance is None
        or left_ss is None
        or right_ss is None
        or left_ss <= 0.0
        or right_ss <= 0.0
    ):
        return None
    denominator = math.sqrt(left_ss * right_ss)
    if not math.isfinite(denominator) or denominator <= 0.0:
        return None
    result = covariance / denominator
    return result if math.isfinite(result) else None


def population_sigma(values: tuple[float, ...]) -> float | None:
    if not _numeric_tuple(values, "values"):
        return None
    mean = _mean(values)
    if mean is None:
        return None
    squared = tuple((value - mean) ** 2 for value in values)
    total = _safe_fsum(squared)
    if total is None:
        return None
    variance = total / len(values)
    result = math.sqrt(variance)
    return result if math.isfinite(result) else None


def _phi_raw(spreads: tuple[float, ...], mu: float) -> tuple[float | None, bool]:
    if not _numeric_tuple(spreads, "spreads"):
        return None, False
    _float(mu, "mu")
    if len(spreads) < 2:
        return None, True
    centered = tuple(value - mu for value in spreads)
    numerator = _safe_fsum(
        tuple(
            centered[index - 1] * centered[index] for index in range(1, len(centered))
        )
    )
    denominator = _safe_fsum(tuple(value * value for value in centered[:-1]))
    if numerator is None or denominator is None:
        return None, False
    if denominator <= 0.0:
        return None, True
    result = numerator / denominator
    return (result if math.isfinite(result) else None), False


def phi_and_half_life(
    spreads: tuple[float, ...], mu: float
) -> tuple[float, float] | None:
    phi, _ = _phi_raw(spreads, mu)
    if phi is None or not 0.0 < phi < 1.0:
        return None
    half_life = math.log(0.5) / math.log(phi)
    if not math.isfinite(half_life):
        return None
    return phi, half_life


@dataclass(frozen=True, slots=True)
class S4Estimate:
    config_id: str
    decision_ts: int
    pair: str
    symbol_a: str
    symbol_b: str
    beta_a: float
    beta_b: float
    beta_a_first: float
    beta_a_second: float
    beta_b_first: float
    beta_b_second: float
    weight_a: float
    weight_b: float
    spread: float
    mu: float
    mad: float
    effective_mad_scale: float
    z: float
    prior_beta_a: float
    prior_beta_b: float
    prior_weight_a: float
    prior_weight_b: float
    prior_mu: float
    prior_mad: float
    prior_effective_mad_scale: float
    z_prior: float
    D_fraction: float
    D_bps: float
    rho: float
    phi: float
    half_life_4h_bars: float
    beta_stability: float
    sigma_pair: float
    pair_return_fraction: float
    pair_return_bps: float
    current_market_return_4h: float

    def __post_init__(self) -> None:
        _str(self.config_id, "config_id")
        _int(self.decision_ts, "decision_ts")
        if _str(self.pair, "pair") not in PAIR_ORDER:
            raise ValueError("pair outside frozen order")
        expected_symbols = _PAIR_SYMBOLS[self.pair]
        if (self.symbol_a, self.symbol_b) != expected_symbols:
            raise ValueError("pair symbol order drift")
        for name in (
            "beta_a",
            "beta_b",
            "beta_a_first",
            "beta_a_second",
            "beta_b_first",
            "beta_b_second",
            "weight_a",
            "weight_b",
            "spread",
            "mu",
            "mad",
            "effective_mad_scale",
            "z",
            "prior_beta_a",
            "prior_beta_b",
            "prior_weight_a",
            "prior_weight_b",
            "prior_mu",
            "prior_mad",
            "prior_effective_mad_scale",
            "z_prior",
            "D_fraction",
            "D_bps",
            "rho",
            "phi",
            "half_life_4h_bars",
            "beta_stability",
            "sigma_pair",
            "pair_return_fraction",
            "pair_return_bps",
            "current_market_return_4h",
        ):
            _float(getattr(self, name), name)
        if not 0.0 < self.phi < 1.0:
            raise ValueError("phi must be in the open unit interval")
        if self.mad < 0.0 or self.prior_mad < 0.0 or self.sigma_pair < 0.0:
            raise ValueError("scale values must not be negative")


@dataclass(frozen=True, slots=True)
class S4EstimationOutcome:
    estimate: S4Estimate | None
    rejection_reason: str | None

    def __post_init__(self) -> None:
        if self.estimate is not None and type(self.estimate) is not S4Estimate:
            raise TypeError("estimate must be exact S4Estimate or None")
        if (self.estimate is None) == (self.rejection_reason is None):
            raise ValueError("outcome must contain exactly estimate or rejection")
        if self.rejection_reason is not None and (
            type(self.rejection_reason) is not str
            or self.rejection_reason not in S4_ESTIMATION_REASONS
        ):
            raise ValueError("unknown S4 estimation reason")


def _reject(reason: str) -> S4EstimationOutcome:
    return S4EstimationOutcome(None, reason)


@dataclass(frozen=True, slots=True)
class _Window:
    returns_a: tuple[float, ...]
    returns_b: tuple[float, ...]
    market: tuple[float, ...]
    closes_a: tuple[float, ...]
    closes_b: tuple[float, ...]


def _feature_return(snapshot: CommonSnapshot, symbol: str) -> float | None:
    return snapshot.features[SYMBOLS.index(symbol)].r


def _window(
    feature_context: FeatureContext,
    snapshots: tuple[CommonSnapshot, ...],
    symbols: tuple[str, str],
) -> _Window | None:
    returns_a = tuple(_feature_return(snapshot, symbols[0]) for snapshot in snapshots)
    returns_b = tuple(_feature_return(snapshot, symbols[1]) for snapshot in snapshots)
    if any(value is None for value in returns_a + returns_b):
        return None
    bars_a = {bar.close_ts: bar.close for bar in feature_context.bars_for(symbols[0])}
    bars_b = {bar.close_ts: bar.close for bar in feature_context.bars_for(symbols[1])}
    try:
        closes_a = tuple(bars_a[snapshot.decision_ts] for snapshot in snapshots)
        closes_b = tuple(bars_b[snapshot.decision_ts] for snapshot in snapshots)
    except KeyError:
        return None
    return _Window(
        tuple(value for value in returns_a if value is not None),
        tuple(value for value in returns_b if value is not None),
        tuple(snapshot.m for snapshot in snapshots),
        closes_a,
        closes_b,
    )


def _spreads(window: _Window, weight_a: float, weight_b: float) -> tuple[float, ...]:
    return tuple(
        weight_a * math.log(close_a) - weight_b * math.log(close_b)
        for close_a, close_b in zip(window.closes_a, window.closes_b, strict=True)
    )


def _all_finite(values: tuple[float, ...]) -> bool:
    return all(type(value) is float and math.isfinite(value) for value in values)


def estimate_s4_pair(
    feature_context: FeatureContext,
    config: S4Config,
    decision_ts: int,
    pair: str,
) -> S4EstimationOutcome:
    """Evaluate one frozen-order pair at one exact complete close."""
    if type(feature_context) is not FeatureContext:
        raise TypeError("feature_context must be exact FeatureContext")
    if type(config) is not S4Config:
        raise TypeError("config must be exact registered S4Config")
    assert_registered_config(config)
    _int(decision_ts, "decision_ts")
    if decision_ts % FOUR_HOUR_MS:
        raise ValueError("decision_ts must be an exact UTC 4h close")
    if _str(pair, "pair") not in PAIR_ORDER:
        raise ValueError("pair outside frozen pair order")
    symbols = _PAIR_SYMBOLS[pair]

    position = next(
        (
            index
            for index, snapshot in enumerate(feature_context.snapshots)
            if snapshot.decision_ts == decision_ts
        ),
        None,
    )
    if position is None or position < config.W:
        return _reject("missing_required_context")
    combined = feature_context.snapshots[position - config.W : position + 1]
    if len(combined) != config.W + 1 or any(
        right.decision_ts != left.decision_ts + FOUR_HOUR_MS
        for left, right in zip(combined, combined[1:], strict=False)
    ):
        return _reject("missing_required_context")
    for symbol in symbols:
        by_close = {bar.close_ts: bar for bar in feature_context.bars_for(symbol)}
        selected = tuple(by_close.get(snapshot.decision_ts) for snapshot in combined)
        if any(bar is None for bar in selected):
            return _reject("missing_required_context")
        exact_bars = tuple(bar for bar in selected if bar is not None)
        if any(
            right.ts != left.close_ts or right.is_segment_start
            for left, right in zip(exact_bars, exact_bars[1:], strict=False)
        ):
            return _reject("missing_required_context")

    prior_window = _window(feature_context, combined[:-1], symbols)
    current_window = _window(feature_context, combined[1:], symbols)
    if prior_window is None or current_window is None:
        return _reject("missing_required_context")
    required_inputs = (
        current_window.returns_a
        + current_window.returns_b
        + current_window.market
        + current_window.closes_a
        + current_window.closes_b
        + prior_window.returns_a
        + prior_window.returns_b
        + prior_window.market
        + prior_window.closes_a
        + prior_window.closes_b
    )
    if not _all_finite(required_inputs):
        return _reject("nonfinite_required_input")

    beta_a = compute_clipped_beta(current_window.returns_a, current_window.market)
    beta_b = compute_clipped_beta(current_window.returns_b, current_window.market)
    prior_beta_a = compute_clipped_beta(prior_window.returns_a, prior_window.market)
    prior_beta_b = compute_clipped_beta(prior_window.returns_b, prior_window.market)
    if None in (beta_a, beta_b, prior_beta_a, prior_beta_b):
        return _reject("degenerate_beta_market_variance")
    if beta_a is None or beta_b is None or prior_beta_a is None or prior_beta_b is None:
        return _reject("degenerate_beta_market_variance")

    half = config.W // 2
    beta_a_first = compute_clipped_beta(
        current_window.returns_a[:half], current_window.market[:half]
    )
    beta_a_second = compute_clipped_beta(
        current_window.returns_a[half:], current_window.market[half:]
    )
    beta_b_first = compute_clipped_beta(
        current_window.returns_b[:half], current_window.market[:half]
    )
    beta_b_second = compute_clipped_beta(
        current_window.returns_b[half:], current_window.market[half:]
    )
    if None in (beta_a_first, beta_a_second, beta_b_first, beta_b_second):
        return _reject("degenerate_beta_market_variance")
    if (
        beta_a_first is None
        or beta_a_second is None
        or beta_b_first is None
        or beta_b_second is None
    ):
        return _reject("degenerate_beta_market_variance")

    rho = correlation(current_window.returns_a, current_window.returns_b)
    if rho is None:
        return _reject("degenerate_rho_variance")
    weight_a = beta_b / (beta_a + beta_b)
    weight_b = beta_a / (beta_a + beta_b)
    prior_weight_a = prior_beta_b / (prior_beta_a + prior_beta_b)
    prior_weight_b = prior_beta_a / (prior_beta_a + prior_beta_b)
    current_spreads = _spreads(current_window, weight_a, weight_b)
    prior_spreads = _spreads(prior_window, prior_weight_a, prior_weight_b)
    if not _all_finite(current_spreads + prior_spreads):
        return _reject("nonfinite_required_input")
    current_stats = spread_statistics(current_spreads)
    prior_stats = spread_statistics(prior_spreads)
    if current_stats is None or prior_stats is None:
        return _reject("nonfinite_required_input")
    z_value = (current_spreads[-1] - current_stats.mu) / current_stats.effective_scale
    z_prior = (prior_spreads[-1] - prior_stats.mu) / prior_stats.effective_scale

    phi, denominator_degenerate = _phi_raw(current_spreads, current_stats.mu)
    if phi is None:
        return _reject(
            "degenerate_phi_denominator"
            if denominator_degenerate
            else "nonfinite_required_input"
        )
    if not 0.0 < phi < 1.0:
        return _reject("phi_not_in_open_unit_interval")
    half_life = math.log(0.5) / math.log(phi)
    if not math.isfinite(half_life):
        return _reject("nonfinite_required_input")

    pair_returns = tuple(
        weight_a * left - weight_b * right
        for left, right in zip(
            current_window.returns_a, current_window.returns_b, strict=True
        )
    )
    sigma_pair = population_sigma(pair_returns)
    if sigma_pair is None:
        return _reject("nonfinite_required_input")
    beta_stability = max(
        abs(beta_a_first - beta_a_second) / beta_a,
        abs(beta_b_first - beta_b_second) / beta_b,
    )
    d_fraction = abs(current_spreads[-1] - current_stats.mu)
    pair_return = pair_returns[-1]
    final_values = (
        weight_a,
        weight_b,
        prior_weight_a,
        prior_weight_b,
        z_value,
        z_prior,
        beta_stability,
        d_fraction,
        pair_return,
    )
    if not _all_finite(final_values):
        return _reject("nonfinite_required_input")

    return S4EstimationOutcome(
        S4Estimate(
            config.config_id,
            decision_ts,
            pair,
            symbols[0],
            symbols[1],
            beta_a,
            beta_b,
            beta_a_first,
            beta_a_second,
            beta_b_first,
            beta_b_second,
            weight_a,
            weight_b,
            current_spreads[-1],
            current_stats.mu,
            current_stats.mad,
            current_stats.effective_scale,
            z_value,
            prior_beta_a,
            prior_beta_b,
            prior_weight_a,
            prior_weight_b,
            prior_stats.mu,
            prior_stats.mad,
            prior_stats.effective_scale,
            z_prior,
            d_fraction,
            d_fraction * 10_000.0,
            rho,
            phi,
            half_life,
            beta_stability,
            sigma_pair,
            pair_return,
            pair_return * 10_000.0,
            combined[-1].m,
        ),
        None,
    )


__all__ = [
    "PAIR_ORDER",
    "S4Estimate",
    "S4EstimationOutcome",
    "S4_ESTIMATION_REASONS",
    "SpreadStatistics",
    "compute_clipped_beta",
    "correlation",
    "estimate_s4_pair",
    "fixed_median",
    "phi_and_half_life",
    "population_sigma",
    "spread_statistics",
]
