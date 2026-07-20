from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import math

import pytest
from rob974_features import Bar4h, CommonSnapshot, SymbolFeature
from rob974_h3_manifest import PAIRS, SYMBOLS, get_config
from rob974_h3_s3 import FeatureContext

H4 = 4 * 60 * 60 * 1000


@pytest.fixture(scope="module")
def s4():
    spec = importlib.util.find_spec("rob974_h3_s4")
    assert spec is not None, "ROB-980 CP4 S4 PIT estimator is not implemented"
    return importlib.import_module("rob974_h3_s4")


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return math.fsum((ordered[middle - 1], ordered[middle])) / 2.0


def _beta(returns, market):
    r_mean = math.fsum(returns) / len(returns)
    m_mean = math.fsum(market) / len(market)
    numerator = math.fsum(
        (r_value - r_mean) * (m_value - m_mean)
        for r_value, m_value in zip(returns, market, strict=True)
    )
    denominator = math.fsum((value - m_mean) ** 2 for value in market)
    return min(max(numerator / denominator, 0.25), 3.0)


def _normal_context(count: int = 121):
    states = []
    for index in range(-1, count):
        common = 0.001 * index + 0.012 * math.sin(index * 0.19)
        states.append(
            (
                math.log(100.0) + 5.0 * common + 0.006 * math.sin(index * 0.31),
                math.log(80.0) + 0.10 * common - 0.004 * math.sin(index * 0.23),
                math.log(60.0) + 1.20 * common + 0.003 * math.sin(index * 0.17),
            )
        )
    bars = {symbol: [] for symbol in SYMBOLS}
    snapshots = []
    for index in range(count):
        current = states[index + 1]
        prior = states[index]
        returns = tuple(
            current[position] - prior[position] for position in range(len(SYMBOLS))
        )
        features = []
        for position, symbol in enumerate(SYMBOLS):
            close = math.exp(current[position])
            bars[symbol].append(
                Bar4h(
                    index * H4,
                    (index + 1) * H4,
                    close,
                    close * 1.001,
                    close * 0.999,
                    close,
                    1.0,
                    index == 0,
                )
            )
            features.append(
                SymbolFeature(
                    symbol,
                    (index + 1) * H4,
                    returns[position],
                    1.0,
                    1.0,
                    0.01,
                    close,
                    close,
                    50.0,
                    0.10,
                )
            )
        snapshots.append(
            CommonSnapshot(
                (index + 1) * H4,
                sorted(returns)[1],
                999.0,
                2,
                1,
                tuple(features),
            )
        )
    return FeatureContext.from_h1(
        {symbol: tuple(values) for symbol, values in bars.items()}, tuple(snapshots)
    )


def _window_values(context, pair, end_index, length):
    left, right = pair.split("-")
    symbols = (f"{left}USDT", f"{right}USDT")
    snapshots = context.snapshots[end_index - length + 1 : end_index + 1]
    returns_a = tuple(
        snapshot.features[SYMBOLS.index(symbols[0])].r for snapshot in snapshots
    )
    returns_b = tuple(
        snapshot.features[SYMBOLS.index(symbols[1])].r for snapshot in snapshots
    )
    market = tuple(snapshot.m for snapshot in snapshots)
    closes = []
    for symbol in symbols:
        by_ts = {bar.close_ts: bar.close for bar in context.bars_for(symbol)}
        closes.append(tuple(by_ts[snapshot.decision_ts] for snapshot in snapshots))
    return returns_a, returns_b, market, tuple(closes)


def _degenerate_context(kind: str, count: int = 121):
    bars = {symbol: [] for symbol in SYMBOLS}
    snapshots = []
    for index in range(count):
        market = 0.0 if kind == "beta" else 0.001 * ((index % 7) - 3)
        if kind == "rho":
            returns = (2.0 * market, 0.001, market)
        else:
            returns = (2.0 * market, market, market)
        features = []
        for position, symbol in enumerate(SYMBOLS):
            close = 100.0 + position * 10.0
            bars[symbol].append(
                Bar4h(
                    index * H4,
                    (index + 1) * H4,
                    close,
                    close,
                    close,
                    close,
                    1.0,
                    index == 0,
                )
            )
            features.append(
                SymbolFeature(
                    symbol,
                    (index + 1) * H4,
                    returns[position],
                    0.0,
                    0.0,
                    0.0,
                    close,
                    close,
                    50.0,
                    0.0,
                )
            )
        snapshots.append(
            CommonSnapshot((index + 1) * H4, market, 999.0, 2, 1, tuple(features))
        )
    return FeatureContext.from_h1(
        {symbol: tuple(values) for symbol, values in bars.items()}, tuple(snapshots)
    )


def test_pair_order_and_exact_fixed_order_estimator_against_independent_oracle(s4):
    assert s4.PAIR_ORDER == PAIRS == ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")
    config = get_config("S4-01")
    context = _normal_context()
    outcome = s4.estimate_s4_pair(context, config, 121 * H4, "XRP-DOGE")
    assert outcome.rejection_reason is None
    estimate = outcome.estimate
    assert estimate is not None
    returns_a, returns_b, market, closes = _window_values(
        context, "XRP-DOGE", 120, config.W
    )
    beta_a = _beta(returns_a, market)
    beta_b = _beta(returns_b, market)
    weight_a = beta_b / (beta_a + beta_b)
    weight_b = beta_a / (beta_a + beta_b)
    spreads = tuple(
        weight_a * math.log(a_close) - weight_b * math.log(b_close)
        for a_close, b_close in zip(*closes, strict=True)
    )
    mu = _median(spreads)
    mad = _median(tuple(abs(value - mu) for value in spreads))
    scale = max(1.4826 * mad, 1e-6)
    centered = tuple(value - mu for value in spreads)
    phi = math.fsum(
        centered[index - 1] * centered[index] for index in range(1, len(centered))
    ) / math.fsum(value * value for value in centered[:-1])
    assert estimate.beta_a == beta_a
    assert estimate.beta_b == beta_b
    assert estimate.weight_a == weight_a
    assert estimate.weight_b == weight_b
    assert estimate.mu == mu
    assert estimate.mad == mad
    assert estimate.effective_mad_scale == scale
    assert estimate.z == (spreads[-1] - mu) / scale
    assert estimate.phi == phi
    assert estimate.half_life_4h_bars == math.log(0.5) / math.log(phi)
    returns_a_centered = tuple(
        value - math.fsum(returns_a) / len(returns_a) for value in returns_a
    )
    returns_b_centered = tuple(
        value - math.fsum(returns_b) / len(returns_b) for value in returns_b
    )
    expected_rho = math.fsum(
        left * right
        for left, right in zip(returns_a_centered, returns_b_centered, strict=True)
    ) / math.sqrt(
        math.fsum(value * value for value in returns_a_centered)
        * math.fsum(value * value for value in returns_b_centered)
    )
    assert estimate.rho == expected_rho
    pair_returns = tuple(
        weight_a * left - weight_b * right
        for left, right in zip(returns_a, returns_b, strict=True)
    )
    pair_mean = math.fsum(pair_returns) / len(pair_returns)
    expected_sigma = math.sqrt(
        math.fsum((value - pair_mean) ** 2 for value in pair_returns)
        / len(pair_returns)
    )
    assert estimate.sigma_pair == expected_sigma
    assert estimate.pair_return_bps == estimate.pair_return_fraction * 10_000.0
    assert estimate.D_bps == estimate.D_fraction * 10_000.0

    prior_a, prior_b, prior_market, prior_closes = _window_values(
        context, "XRP-DOGE", 119, config.W
    )
    prior_beta_a = _beta(prior_a, prior_market)
    prior_beta_b = _beta(prior_b, prior_market)
    prior_weight_a = prior_beta_b / (prior_beta_a + prior_beta_b)
    prior_weight_b = prior_beta_a / (prior_beta_a + prior_beta_b)
    prior_spreads = tuple(
        prior_weight_a * math.log(a_close) - prior_weight_b * math.log(b_close)
        for a_close, b_close in zip(*prior_closes, strict=True)
    )
    prior_mu = _median(prior_spreads)
    prior_mad = _median(tuple(abs(value - prior_mu) for value in prior_spreads))
    prior_scale = max(1.4826 * prior_mad, 1e-6)
    assert (
        estimate.prior_beta_a,
        estimate.prior_beta_b,
        estimate.prior_weight_a,
        estimate.prior_weight_b,
        estimate.prior_mu,
        estimate.prior_mad,
        estimate.prior_effective_mad_scale,
        estimate.z_prior,
    ) == (
        prior_beta_a,
        prior_beta_b,
        prior_weight_a,
        prior_weight_b,
        prior_mu,
        prior_mad,
        prior_scale,
        (prior_spreads[-1] - prior_mu) / prior_scale,
    )


def test_prior_z_is_recomputed_from_prior_own_window_and_ignores_t_values(s4):
    config = get_config("S4-01")
    context = _normal_context()
    baseline = s4.estimate_s4_pair(context, config, 121 * H4, "XRP-DOGE").estimate
    assert baseline is not None
    bars = {symbol: context.bars_for(symbol) for symbol in SYMBOLS}
    changed_xrp = list(bars["XRPUSDT"])
    current = changed_xrp[-1]
    changed_xrp[-1] = dataclasses.replace(
        current,
        open=current.open * 1.01,
        high=current.high * 1.02,
        low=current.low,
        close=current.close * 1.01,
    )
    bars["XRPUSDT"] = tuple(changed_xrp)
    changed = FeatureContext.from_h1(bars, context.snapshots)
    mutated = s4.estimate_s4_pair(changed, config, 121 * H4, "XRP-DOGE").estimate
    assert mutated is not None
    assert mutated.z != baseline.z
    assert (
        mutated.z_prior,
        mutated.prior_beta_a,
        mutated.prior_beta_b,
        mutated.prior_weight_a,
        mutated.prior_weight_b,
        mutated.prior_mu,
        mutated.prior_effective_mad_scale,
    ) == (
        baseline.z_prior,
        baseline.prior_beta_a,
        baseline.prior_beta_b,
        baseline.prior_weight_a,
        baseline.prior_weight_b,
        baseline.prior_mu,
        baseline.prior_effective_mad_scale,
    )


def test_lowercase_m_is_beta_authority_and_uppercase_M_is_irrelevant(s4):
    config = get_config("S4-01")
    context = _normal_context()
    first = s4.estimate_s4_pair(context, config, 121 * H4, "XRP-DOGE")
    snapshots = tuple(
        dataclasses.replace(snapshot, M=-999.0) for snapshot in context.snapshots
    )
    changed = s4.estimate_s4_pair(
        FeatureContext.from_h1(
            {symbol: context.bars_for(symbol) for symbol in SYMBOLS}, snapshots
        ),
        config,
        121 * H4,
        "XRP-DOGE",
    )
    assert changed == first


def test_mad_zero_uses_only_effective_scale_floor_and_sigma_zero_remains_zero(s4):
    spreads = (0.0,) * 119 + (0.01,)
    stats = s4.spread_statistics(spreads)
    assert stats is not None
    assert stats.mad == 0.0
    assert stats.effective_scale == 1e-6
    assert s4.population_sigma((0.0,) * 120) == 0.0


def test_degenerate_and_nonfinite_numeric_inputs_reject_deterministically(s4):
    assert s4.compute_clipped_beta((1.0, 2.0), (0.0, 0.0)) is None
    assert s4.correlation((1.0, 1.0), (1.0, 2.0)) is None
    assert s4.phi_and_half_life((1.0, 1.0, 1.0), 1.0) is None
    assert s4.spread_statistics((0.0, math.nan)) is None
    assert s4.population_sigma((0.0, math.inf)) is None

    config = get_config("S4-01")
    beta = s4.estimate_s4_pair(
        _degenerate_context("beta"), config, 121 * H4, "XRP-DOGE"
    )
    assert beta.rejection_reason == "degenerate_beta_market_variance"
    rho = s4.estimate_s4_pair(_degenerate_context("rho"), config, 121 * H4, "XRP-DOGE")
    assert rho.rejection_reason == "degenerate_rho_variance"
    phi = s4.estimate_s4_pair(_degenerate_context("phi"), config, 121 * H4, "XRP-DOGE")
    assert phi.rejection_reason == "degenerate_phi_denominator"


def test_clipped_beta_definition_is_identical_for_full_and_halves(s4):
    config = get_config("S4-01")
    estimate = s4.estimate_s4_pair(
        _normal_context(), config, 121 * H4, "XRP-DOGE"
    ).estimate
    assert estimate is not None
    assert estimate.beta_a == 3.0
    assert estimate.beta_b == 0.25
    returns_a, returns_b, market, _ = _window_values(
        _normal_context(), "XRP-DOGE", 120, config.W
    )
    half = config.W // 2
    assert estimate.beta_a_first == _beta(returns_a[:half], market[:half])
    assert estimate.beta_a_second == _beta(returns_a[half:], market[half:])
    assert estimate.beta_b_first == _beta(returns_b[:half], market[:half])
    assert estimate.beta_b_second == _beta(returns_b[half:], market[half:])


def test_exact_types_and_invalid_pair_fail_closed(s4):
    context = _normal_context()
    config = get_config("S4-01")
    with pytest.raises(TypeError):
        s4.estimate_s4_pair(context, config, True, "XRP-DOGE")
    with pytest.raises(ValueError):
        s4.estimate_s4_pair(context, config, 121 * H4, "DOGE-XRP")
