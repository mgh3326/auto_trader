"""ROB-974 H3 S4 exact point-in-time pair estimator.

Every evaluation uses its own W-wide beta-neutral weights over its historical
spread.  The stored prior z-score is a separate t-1 evaluation and therefore
cannot consume t betas, weights, closes, median, MAD, or scale.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from rob974_features import FOUR_HOUR_MS, MINUTE_MS, CommonSnapshot
from rob974_h3_manifest import (
    PAIRS,
    S4_GENERATOR_REJECTION_TAXONOMY,
    S4_NO_SIGNAL_TAXONOMY,
    SYMBOLS,
    S4Config,
    assert_registered_config,
)
from rob974_h3_s3 import EmitWindow, FeatureContext, expected_decision_closes

PAIR_ORDER: tuple[str, ...] = PAIRS
_PAIR_SYMBOLS = {
    "XRP-DOGE": ("XRPUSDT", "DOGEUSDT"),
    "XRP-SOL": ("XRPUSDT", "SOLUSDT"),
    "DOGE-SOL": ("DOGEUSDT", "SOLUSDT"),
}
S4_ESTIMATION_REASONS: tuple[str, ...] = S4_NO_SIGNAL_TAXONOMY[:6]


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


S4_NO_SIGNAL_REASONS: tuple[str, ...] = S4_NO_SIGNAL_TAXONOMY
S4_GENERATOR_REJECTION_REASONS: tuple[str, ...] = S4_GENERATOR_REJECTION_TAXONOMY
HISTORICAL_NOTIONAL_ASSUMPTION = "frozen_continuous_6_to_10_usd_per_leg"


def s4_risk_distances(config: S4Config, sigma_pair: float) -> tuple[float, float]:
    if type(config) is not S4Config:
        raise TypeError("config must be exact registered S4Config")
    assert_registered_config(config)
    _float(sigma_pair, "sigma_pair")
    if sigma_pair < 0.0:
        raise ValueError("sigma_pair must not be negative")
    d_sl = min(max(config.k_SL * sigma_pair, 0.008), 0.016)
    d_tp = max(0.0068, config.R_TP * d_sl)
    return d_sl, d_tp


@dataclass(frozen=True, slots=True)
class HistoricalNotional:
    G_min: float
    G_max: float
    G: float | None

    def __post_init__(self) -> None:
        _float(self.G_min, "G_min")
        _float(self.G_max, "G_max")
        if self.G is not None:
            _float(self.G, "G")
            if self.G != self.G_min or self.G_min > self.G_max:
                raise ValueError("feasible historical G must be exact G_min")
        elif self.G_min <= self.G_max:
            raise ValueError("feasible historical sizing cannot omit G")


def historical_notional(weight_a: float, weight_b: float) -> HistoricalNotional:
    _float(weight_a, "weight_a")
    _float(weight_b, "weight_b")
    if weight_a <= 0.0 or weight_b <= 0.0:
        raise ValueError("historical weights must be positive")
    g_min = max(6.0 / weight_a, 6.0 / weight_b)
    g_max = min(10.0 / weight_a, 10.0 / weight_b)
    if not math.isfinite(g_min) or not math.isfinite(g_max):
        raise ValueError("nonfinite historical notional")
    return HistoricalNotional(g_min, g_max, g_min if g_min <= g_max else None)


@dataclass(frozen=True, slots=True)
class S4Candidate:
    """Dedicated H3 historical pair intent; it is never split into two intents."""

    strategy: str
    config_id: str
    decision_ts: int
    pair: str
    side: str
    symbol_a: str
    symbol_b: str
    side_a: str
    side_b: str
    beta_a: float
    beta_b: float
    weight_a: float
    weight_b: float
    mu: float
    mad: float
    effective_mad_scale: float
    observed_z: float
    prior_observed_z: float
    D_fraction: float
    D_bps: float
    rho: float
    half_life_4h_bars: float
    beta_stability: float
    sigma_pair_risk: float
    observed_pair_return_fraction: float
    gross_notional_usd: float
    notional_a_usd: float
    notional_b_usd: float
    d_SL: float
    d_TP: float
    historical_notional_assumption: str
    historical_eligibility: bool
    historical_eligibility_authority: str
    volatility_percentile: None
    volatility_percentile_provenance: str
    entry_tick_ts: int
    entry_deadline_ts: int
    max_hold_4h_bars: int
    leg_a_order_id: None
    leg_b_order_id: None
    leg_a_fill_id: None
    leg_b_fill_id: None
    pair_executor_provenance: str

    def __post_init__(self) -> None:
        if _str(self.strategy, "strategy") != "S4":
            raise ValueError("S4 candidate strategy must be S4")
        _str(self.config_id, "config_id")
        _int(self.decision_ts, "decision_ts")
        if _str(self.pair, "pair") not in PAIR_ORDER:
            raise ValueError("candidate pair outside frozen order")
        if (self.symbol_a, self.symbol_b) != _PAIR_SYMBOLS[self.pair]:
            raise ValueError("candidate pair symbol order drift")
        if _str(self.side, "side") not in (
            "short_a_long_b",
            "long_a_short_b",
        ):
            raise ValueError("invalid pair side")
        if (self.side_a, self.side_b) not in (("short", "long"), ("long", "short")):
            raise ValueError("invalid frozen leg directions")
        if self.side == "short_a_long_b" and (self.side_a, self.side_b) != (
            "short",
            "long",
        ):
            raise ValueError("positive-z direction mismatch")
        if self.side == "long_a_short_b" and (self.side_a, self.side_b) != (
            "long",
            "short",
        ):
            raise ValueError("negative-z direction mismatch")
        for name in (
            "beta_a",
            "beta_b",
            "weight_a",
            "weight_b",
            "mu",
            "mad",
            "effective_mad_scale",
            "observed_z",
            "prior_observed_z",
            "D_fraction",
            "D_bps",
            "rho",
            "half_life_4h_bars",
            "beta_stability",
            "sigma_pair_risk",
            "observed_pair_return_fraction",
            "gross_notional_usd",
            "notional_a_usd",
            "notional_b_usd",
            "d_SL",
            "d_TP",
        ):
            _float(getattr(self, name), name)
        if (
            _str(self.historical_notional_assumption, "historical_notional_assumption")
            != HISTORICAL_NOTIONAL_ASSUMPTION
        ):
            raise ValueError("historical notional authority drift")
        if (
            type(self.historical_eligibility) is not bool
            or not self.historical_eligibility
        ):
            raise ValueError("S4 requires frozen historical eligibility")
        if (
            _str(
                self.historical_eligibility_authority,
                "historical_eligibility_authority",
            )
            != "rob974_h1_parent_manifest_selected_universe"
        ):
            raise ValueError("historical eligibility authority drift")
        if self.volatility_percentile is not None:
            raise ValueError("S4 volatility percentile must be exactly null")
        if (
            _str(
                self.volatility_percentile_provenance,
                "volatility_percentile_provenance",
            )
            != "not_defined_for_s4"
        ):
            raise ValueError("S4 volatility provenance drift")
        _int(self.entry_tick_ts, "entry_tick_ts")
        _int(self.entry_deadline_ts, "entry_deadline_ts")
        _int(self.max_hold_4h_bars, "max_hold_4h_bars")
        if self.entry_tick_ts != self.decision_ts:
            raise ValueError("entry tick must equal decision close")
        if self.entry_deadline_ts != self.decision_ts + MINUTE_MS:
            raise ValueError("entry deadline must bound the exact next minute")
        if self.max_hold_4h_bars != 9:
            raise ValueError("S4 maximum hold is frozen at nine 4h bars")
        if self.notional_a_usd != self.weight_a * self.gross_notional_usd or (
            self.notional_b_usd != self.weight_b * self.gross_notional_usd
        ):
            raise ValueError("leg notionals must use frozen weights and gross G")
        if not (
            6.0 <= self.notional_a_usd <= 10.0 and 6.0 <= self.notional_b_usd <= 10.0
        ):
            raise ValueError("leg notionals must remain in the frozen $6-10 range")
        if any(
            value is not None
            for value in (
                self.leg_a_order_id,
                self.leg_b_order_id,
                self.leg_a_fill_id,
                self.leg_b_fill_id,
            )
        ):
            raise ValueError("H3 historical order/fill identifiers must be null")
        if (
            _str(self.pair_executor_provenance, "pair_executor_provenance")
            != "not_evaluated_h3_generator"
        ):
            raise ValueError("pair executor evidence must not be fabricated")

    @property
    def signal_ts(self) -> int:
        return self.decision_ts

    @property
    def identity(self) -> tuple[str, str, int, str, str]:
        return (
            self.strategy,
            self.config_id,
            self.decision_ts,
            self.pair,
            self.side,
        )


@dataclass(frozen=True, slots=True)
class S4GateOutcome:
    side: str | None
    candidate: S4Candidate | None
    no_signal_reason: str | None

    def __post_init__(self) -> None:
        if self.side is not None and self.side not in (
            "short_a_long_b",
            "long_a_short_b",
        ):
            raise ValueError("invalid S4 outcome side")
        if self.candidate is not None and type(self.candidate) is not S4Candidate:
            raise TypeError("candidate must be exact S4Candidate or None")
        if (self.candidate is None) == (self.no_signal_reason is None):
            raise ValueError("gate outcome must contain exactly candidate or reason")
        if self.no_signal_reason is not None and (
            type(self.no_signal_reason) is not str
            or self.no_signal_reason not in S4_NO_SIGNAL_REASONS
        ):
            raise ValueError("unknown S4 no-signal reason")


def _gate_reject(reason: str, side: str | None = None) -> S4GateOutcome:
    return S4GateOutcome(side, None, reason)


def evaluate_s4_gates(estimate: S4Estimate, config: S4Config) -> S4GateOutcome:
    """Evaluate convergence and registered S4 eligibility in frozen order."""
    if type(estimate) is not S4Estimate:
        raise TypeError("estimate must be exact S4Estimate")
    if type(config) is not S4Config:
        raise TypeError("config must be exact registered S4Config")
    assert_registered_config(config)
    if estimate.config_id != config.config_id:
        raise ValueError("estimate/config identity mismatch")

    if (
        estimate.z == 0.0
        or estimate.z_prior == 0.0
        or math.copysign(1.0, estimate.z) != math.copysign(1.0, estimate.z_prior)
    ):
        return _gate_reject("convergence_sign")
    side = "short_a_long_b" if estimate.z > 0.0 else "long_a_short_b"
    if abs(estimate.z_prior) < config.z_entry:
        return _gate_reject("prior_z_entry", side)
    if abs(estimate.z) < config.z_entry:
        return _gate_reject("current_z_entry", side)
    if abs(estimate.z) > 0.90 * abs(estimate.z_prior):
        return _gate_reject("convergence_fraction", side)
    if estimate.rho < 0.60:
        return _gate_reject("rho", side)
    if not 2.0 <= estimate.half_life_4h_bars <= 12.0:
        return _gate_reject("half_life", side)
    if estimate.beta_stability > 0.20:
        return _gate_reject("beta_stability", side)
    if estimate.D_bps < float(config.d_min_bp):
        return _gate_reject("absolute_distance", side)
    d_sl, d_tp = s4_risk_distances(config, estimate.sigma_pair)
    if estimate.D_fraction < 1.25 * d_tp:
        return _gate_reject("distance_to_tp", side)
    sizing = historical_notional(estimate.weight_a, estimate.weight_b)
    if sizing.G is None:
        return _gate_reject("historical_notional_feasibility", side)

    side_a, side_b = (
        ("short", "long") if side == "short_a_long_b" else ("long", "short")
    )
    return S4GateOutcome(
        side,
        S4Candidate(
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
            HISTORICAL_NOTIONAL_ASSUMPTION,
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


@dataclass(frozen=True, slots=True)
class S4RejectedCandidate:
    candidate: S4Candidate
    reason: str

    def __post_init__(self) -> None:
        if type(self.candidate) is not S4Candidate:
            raise TypeError("rejected candidate must be exact S4Candidate")
        if (
            type(self.reason) is not str
            or self.reason not in S4_GENERATOR_REJECTION_REASONS
        ):
            raise ValueError("unknown S4 generator-rejection reason")


@dataclass(frozen=True, slots=True)
class S4ArbitrationResult:
    winner: S4Candidate
    rejected: tuple[S4RejectedCandidate, ...]

    def __post_init__(self) -> None:
        if type(self.winner) is not S4Candidate:
            raise TypeError("winner must be exact S4Candidate")
        if type(self.rejected) is not tuple or any(
            type(item) is not S4RejectedCandidate for item in self.rejected
        ):
            raise TypeError("rejected must be tuple of exact records")
        rejected_ids = tuple(item.candidate.identity for item in self.rejected)
        if self.winner.identity in rejected_ids or len(rejected_ids) != len(
            set(rejected_ids)
        ):
            raise ValueError("accepted/rejected pair collision")


def _s4_rank(candidate: S4Candidate) -> tuple[float, float, float, str]:
    return (
        -candidate.D_fraction,
        -abs(candidate.observed_z),
        -candidate.rho,
        candidate.pair,
    )


def arbitrate_s4_candidates(
    candidates: Sequence[S4Candidate],
) -> S4ArbitrationResult:
    if not isinstance(candidates, Sequence):
        raise TypeError("candidates must be a sequence")
    exact = tuple(candidates)
    if not exact:
        raise ValueError("cannot arbitrate an empty pair candidate set")
    if any(type(candidate) is not S4Candidate for candidate in exact):
        raise TypeError("candidates must contain exact S4Candidate values")
    scope = {
        (candidate.strategy, candidate.config_id, candidate.decision_ts)
        for candidate in exact
    }
    if len(scope) != 1:
        raise ValueError("simultaneous pair arbitration scope mismatch")
    identities = tuple(candidate.identity for candidate in exact)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate simultaneous pair candidate identity")
    ordered = tuple(sorted(exact, key=_s4_rank))
    return S4ArbitrationResult(
        ordered[0],
        tuple(
            S4RejectedCandidate(candidate, "simultaneous_pair_arbitration_loser")
            for candidate in ordered[1:]
        ),
    )


@dataclass(frozen=True, slots=True)
class S4UnitDecision:
    decision_ts: int
    pair: str
    status: str
    side: str | None
    candidate: S4Candidate | None
    no_signal_reason: str | None
    generator_rejection_reason: str | None

    def __post_init__(self) -> None:
        _int(self.decision_ts, "decision_ts")
        if _str(self.pair, "pair") not in PAIR_ORDER:
            raise ValueError("decision pair outside frozen order")
        if _str(self.status, "status") not in (
            "NO_SIGNAL",
            "GENERATOR_REJECTED",
            "GENERATOR_ACCEPTED",
        ):
            raise ValueError("unknown S4 unit status")
        if self.side is not None and self.side not in (
            "short_a_long_b",
            "long_a_short_b",
        ):
            raise ValueError("invalid decision side")
        if self.candidate is not None and type(self.candidate) is not S4Candidate:
            raise TypeError("candidate must be exact S4Candidate or None")
        if self.no_signal_reason is not None and (
            type(self.no_signal_reason) is not str
            or self.no_signal_reason not in S4_NO_SIGNAL_REASONS
        ):
            raise ValueError("unknown no-signal reason")
        if self.generator_rejection_reason is not None and (
            type(self.generator_rejection_reason) is not str
            or self.generator_rejection_reason not in S4_GENERATOR_REJECTION_REASONS
        ):
            raise ValueError("unknown generator-rejection reason")


@dataclass(frozen=True, slots=True)
class S4GeneratorOutput:
    strategy: str
    config_id: str
    decisions: tuple[S4UnitDecision, ...]
    accepted: tuple[S4Candidate, ...]
    rejected: tuple[S4RejectedCandidate, ...]

    def __post_init__(self) -> None:
        if _str(self.strategy, "strategy") != "S4":
            raise ValueError("S4 generator output strategy must be S4")
        _str(self.config_id, "config_id")
        if any(item.config_id != self.config_id for item in self.accepted) or any(
            item.candidate.config_id != self.config_id for item in self.rejected
        ):
            raise ValueError("S4 generator output config mismatch")
        if type(self.decisions) is not tuple or type(self.accepted) is not tuple:
            raise TypeError("generator output containers must be tuples")
        if type(self.rejected) is not tuple:
            raise TypeError("generator rejected container must be tuple")
        if any(type(item) is not S4UnitDecision for item in self.decisions):
            raise TypeError("decisions must contain exact S4UnitDecision")
        if any(type(item) is not S4Candidate for item in self.accepted):
            raise TypeError("accepted must contain exact S4Candidate")
        if any(type(item) is not S4RejectedCandidate for item in self.rejected):
            raise TypeError("rejected must contain exact S4RejectedCandidate")
        accepted_ids = {item.identity for item in self.accepted}
        rejected_ids = {item.candidate.identity for item in self.rejected}
        if accepted_ids & rejected_ids:
            raise ValueError("accepted/rejected pair collision")


def generate_s4_global(
    feature_context: FeatureContext,
    emit_window: EmitWindow,
    config: S4Config,
) -> S4GeneratorOutput:
    """Run one historical S4 invocation across all expected closes/pairs."""
    if type(feature_context) is not FeatureContext:
        raise TypeError("feature_context must be exact FeatureContext")
    if type(config) is not S4Config:
        raise TypeError("config must be exact registered S4Config")
    assert_registered_config(config)
    decisions: list[S4UnitDecision] = []
    accepted: list[S4Candidate] = []
    rejected: list[S4RejectedCandidate] = []
    for decision_ts in expected_decision_closes(emit_window):
        outcomes: dict[str, S4GateOutcome] = {}
        candidates: list[S4Candidate] = []
        for pair in PAIR_ORDER:
            estimation = estimate_s4_pair(feature_context, config, decision_ts, pair)
            if estimation.estimate is None:
                if estimation.rejection_reason is None:
                    raise AssertionError("invalid S4 estimation outcome")
                outcome = _gate_reject(estimation.rejection_reason)
            else:
                outcome = evaluate_s4_gates(estimation.estimate, config)
            outcomes[pair] = outcome
            if outcome.candidate is not None:
                candidates.append(outcome.candidate)
        arbitration = arbitrate_s4_candidates(candidates) if candidates else None
        winner_id = arbitration.winner.identity if arbitration is not None else None
        loser_by_id = (
            {item.candidate.identity: item for item in arbitration.rejected}
            if arbitration is not None
            else {}
        )
        if arbitration is not None:
            accepted.append(arbitration.winner)
            rejected.extend(arbitration.rejected)
        for pair in PAIR_ORDER:
            outcome = outcomes[pair]
            candidate = outcome.candidate
            if candidate is None:
                decisions.append(
                    S4UnitDecision(
                        decision_ts,
                        pair,
                        "NO_SIGNAL",
                        outcome.side,
                        None,
                        outcome.no_signal_reason,
                        None,
                    )
                )
            elif candidate.identity == winner_id:
                decisions.append(
                    S4UnitDecision(
                        decision_ts,
                        pair,
                        "GENERATOR_ACCEPTED",
                        candidate.side,
                        candidate,
                        None,
                        None,
                    )
                )
            else:
                loser = loser_by_id[candidate.identity]
                decisions.append(
                    S4UnitDecision(
                        decision_ts,
                        pair,
                        "GENERATOR_REJECTED",
                        candidate.side,
                        candidate,
                        None,
                        loser.reason,
                    )
                )
    return S4GeneratorOutput(
        "S4", config.config_id, tuple(decisions), tuple(accepted), tuple(rejected)
    )


__all__ = [
    "HISTORICAL_NOTIONAL_ASSUMPTION",
    "HistoricalNotional",
    "PAIR_ORDER",
    "S4ArbitrationResult",
    "S4Candidate",
    "S4Estimate",
    "S4EstimationOutcome",
    "S4GateOutcome",
    "S4GeneratorOutput",
    "S4RejectedCandidate",
    "S4UnitDecision",
    "S4_ESTIMATION_REASONS",
    "S4_GENERATOR_REJECTION_REASONS",
    "S4_NO_SIGNAL_REASONS",
    "SpreadStatistics",
    "arbitrate_s4_candidates",
    "compute_clipped_beta",
    "correlation",
    "estimate_s4_pair",
    "evaluate_s4_gates",
    "fixed_median",
    "generate_s4_global",
    "historical_notional",
    "phi_and_half_life",
    "population_sigma",
    "s4_risk_distances",
    "spread_statistics",
]
