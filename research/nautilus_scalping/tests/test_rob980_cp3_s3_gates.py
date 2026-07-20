from __future__ import annotations

import dataclasses
import math

import pytest
import rob974_h3_s3 as s3
from rob974_features import Bar4h, CommonSnapshot, SymbolFeature
from rob974_h3_manifest import FROZEN_S3_CONFIGS, SYMBOLS, get_config

H4 = 4 * 60 * 60 * 1000


def test_cp3_gate_behavior_exists_on_the_importable_cp2_core():
    assert hasattr(s3, "evaluate_s3_gates"), (
        "ROB-980 CP3 S3 gate behavior is not implemented"
    )


def _long(**changes):
    config = get_config("S3-00")
    _, d_tp = s3.s3_risk_distances(config, 0.006)
    values = {
        "config_id": config.config_id,
        "decision_ts": 144_000_000,
        "symbol": "XRPUSDT",
        "R": 0.10,
        "ER": 0.35,
        "S": 1.25,
        "Qplus": 0.35,
        "Qminus": -0.10,
        "close": 101.0,
        "previous_close": 100.0,
        "prior_l_high": 102.0,
        "prior_l_low": 90.0,
        "atr20": 0.6,
        "A": 0.006,
        "vwap12": 100.0,
        "vwap24": 99.0,
        "percentile_30d": 20.0,
        "range24": d_tp / 0.60,
        "market_return_24h": 0.0075,
        "current_market_return_4h": -0.50,
        "bplus": 2,
        "bminus": 1,
    }
    values.update(changes)
    return s3.S3Metrics(**values)


def _short(**changes):
    config = get_config("S3-00")
    _, d_tp = s3.s3_risk_distances(config, 0.006)
    values = {
        "config_id": config.config_id,
        "decision_ts": 144_000_000,
        "symbol": "XRPUSDT",
        "R": -0.10,
        "ER": 0.35,
        "S": -1.25,
        "Qplus": -0.10,
        "Qminus": 0.35,
        "close": 99.0,
        "previous_close": 100.0,
        "prior_l_high": 110.0,
        "prior_l_low": 98.0,
        "atr20": 0.6,
        "A": 0.006,
        "vwap12": 100.0,
        "vwap24": 101.0,
        "percentile_30d": 20.0,
        "range24": d_tp / 0.60,
        "market_return_24h": -0.0075,
        "current_market_return_4h": 0.50,
        "bplus": 1,
        "bminus": 2,
    }
    values.update(changes)
    return s3.S3Metrics(**values)


def _reason(metrics):
    return s3.evaluate_s3_gates(metrics, get_config("S3-00")).no_signal_reason


def test_long_inclusive_boundaries_and_strict_one_ulp_inside_pass():
    exact = _long(
        Qplus=1.25,
        percentile_30d=90.0,
        vwap12=math.nextafter(101.0, -math.inf),
        previous_close=math.nextafter(101.0, -math.inf),
        prior_l_high=math.nextafter(101.0, math.inf),
    )
    outcome = s3.evaluate_s3_gates(exact, get_config("S3-00"))
    assert outcome.no_signal_reason is None
    assert outcome.candidate is not None
    assert outcome.side == "long"


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"market_return_24h": math.nextafter(0.0075, -math.inf)}, "market_regime"),
        ({"bplus": 1}, "market_breadth"),
        ({"S": math.nextafter(1.25, -math.inf)}, "trend_strength"),
        ({"ER": math.nextafter(0.35, -math.inf)}, "efficiency"),
        ({"Qplus": math.nextafter(0.35, -math.inf)}, "pullback_depth"),
        ({"Qplus": math.nextafter(1.25, math.inf)}, "pullback_depth"),
        ({"close": 100.0}, "vwap_reclaim"),
        ({"previous_close": 101.0}, "momentum"),
        ({"prior_l_high": 101.0}, "prior_l_non_breakout"),
        ({"percentile_30d": math.nextafter(20.0, -math.inf)}, "volatility_percentile"),
        ({"percentile_30d": math.nextafter(90.0, math.inf)}, "volatility_percentile"),
    ),
)
def test_long_each_boundary_one_ulp_outside_or_strict_equality_fails(changes, reason):
    assert _reason(_long(**changes)) == reason


def test_long_range_capacity_equality_passes_and_one_ulp_outside_fails():
    metrics = _long()
    outcome = s3.evaluate_s3_gates(metrics, get_config("S3-00"))
    assert outcome.candidate is not None
    assert outcome.candidate.d_TP == 0.60 * metrics.range24
    smaller = math.nextafter(metrics.range24, -math.inf)
    assert _reason(dataclasses.replace(metrics, range24=smaller)) == "range_tp_capacity"


def test_short_inclusive_boundaries_and_strict_one_ulp_inside_pass():
    exact = _short(
        Qminus=1.25,
        percentile_30d=90.0,
        vwap12=math.nextafter(99.0, math.inf),
        previous_close=math.nextafter(99.0, math.inf),
        prior_l_low=math.nextafter(99.0, -math.inf),
    )
    outcome = s3.evaluate_s3_gates(exact, get_config("S3-00"))
    assert outcome.no_signal_reason is None
    assert outcome.candidate is not None
    assert outcome.side == "short"


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"market_return_24h": math.nextafter(-0.0075, math.inf)}, "market_regime"),
        ({"bminus": 1}, "market_breadth"),
        ({"S": math.nextafter(-1.25, math.inf)}, "trend_strength"),
        ({"ER": math.nextafter(0.35, -math.inf)}, "efficiency"),
        ({"Qminus": math.nextafter(0.35, -math.inf)}, "pullback_depth"),
        ({"Qminus": math.nextafter(1.25, math.inf)}, "pullback_depth"),
        ({"close": 100.0}, "vwap_reclaim"),
        ({"previous_close": 99.0}, "momentum"),
        ({"prior_l_low": 99.0}, "prior_l_non_breakout"),
        ({"percentile_30d": math.nextafter(20.0, -math.inf)}, "volatility_percentile"),
        ({"percentile_30d": math.nextafter(90.0, math.inf)}, "volatility_percentile"),
    ),
)
def test_short_each_boundary_one_ulp_outside_or_strict_equality_fails(changes, reason):
    assert _reason(_short(**changes)) == reason


def test_short_range_capacity_equality_passes_and_one_ulp_outside_fails():
    metrics = _short()
    outcome = s3.evaluate_s3_gates(metrics, get_config("S3-00"))
    assert outcome.candidate is not None
    assert outcome.candidate.d_TP == 0.60 * metrics.range24
    smaller = math.nextafter(metrics.range24, -math.inf)
    assert _reason(dataclasses.replace(metrics, range24=smaller)) == "range_tp_capacity"


def test_exact_sl_tp_clips_and_registered_floors_for_every_config():
    baseline = get_config("S3-00")
    assert s3.s3_risk_distances(baseline, 0.0) == (0.008, 0.0128)
    assert s3.s3_risk_distances(baseline, 0.01) == (0.0125, 0.020000000000000004)
    assert s3.s3_risk_distances(baseline, 1.0) == (0.020, 0.032)
    floors = tuple(s3.s3_risk_distances(config, 0.0)[1] for config in FROZEN_S3_CONFIGS)
    assert min(floors) == 0.0108
    assert floors[0] == 0.0128


def _candidate(symbol: str, *, strength: float, efficiency: float, a_value: float):
    _, d_tp = s3.s3_risk_distances(get_config("S3-00"), a_value)
    outcome = s3.evaluate_s3_gates(
        _long(
            symbol=symbol,
            S=strength,
            ER=efficiency,
            A=a_value,
            range24=d_tp / 0.60,
        ),
        get_config("S3-00"),
    )
    assert outcome.candidate is not None
    return outcome.candidate


@pytest.mark.parametrize(
    ("candidate_specs", "winner"),
    (
        (
            (
                ("XRPUSDT", 1.50, 0.50, 0.010),
                ("DOGEUSDT", 1.60, 0.35, 0.006),
            ),
            "DOGEUSDT",
        ),
        (
            (
                ("XRPUSDT", 1.50, 0.40, 0.010),
                ("DOGEUSDT", 1.50, 0.50, 0.006),
            ),
            "DOGEUSDT",
        ),
        (
            (
                ("XRPUSDT", 1.50, 0.50, 0.010),
                ("DOGEUSDT", 1.50, 0.50, 0.011),
            ),
            "DOGEUSDT",
        ),
        (
            tuple((symbol, 1.50, 0.50, 0.010) for symbol in reversed(SYMBOLS)),
            "DOGEUSDT",
        ),
    ),
)
def test_global_arbitration_exact_rank_tiers(candidate_specs, winner):
    candidates = tuple(
        _candidate(
            symbol,
            strength=strength,
            efficiency=efficiency,
            a_value=a_value,
        )
        for symbol, strength, efficiency, a_value in candidate_specs
    )
    result = s3.arbitrate_s3_candidates(candidates)
    assert result.winner.symbol == winner
    assert len(result.rejected) == len(candidates) - 1
    assert all(
        item.reason == "simultaneous_candidate_arbitration_loser"
        for item in result.rejected
    )
    assert {item.candidate.identity for item in result.rejected}.isdisjoint(
        {result.winner.identity}
    )


def test_h3_owned_accepted_payload_is_frozen_complete_and_h2_independent():
    candidate = _candidate("XRPUSDT", strength=1.50, efficiency=0.50, a_value=0.010)
    assert candidate.strategy == "S3"
    assert candidate.signal_ts == candidate.decision_ts
    assert candidate.entry_tick_ts == candidate.decision_ts
    assert candidate.entry_deadline_ts == candidate.decision_ts + 60_000
    assert candidate.max_hold_4h_bars == 12
    assert candidate.volatility_percentile_provenance == "h1_percentile_30d"
    assert candidate.side == "long"
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.side = "short"


def test_real_global_api_accepts_one_and_rejects_every_simultaneous_loser():
    config = get_config("S3-01")
    bars = {}
    snapshots = []
    for symbol_offset, symbol in enumerate(SYMBOLS):
        offset = float(symbol_offset * 10)
        bars[symbol] = tuple(
            Bar4h(
                index * H4,
                (index + 1) * H4,
                99.5 + index + offset,
                102.0 + index + offset,
                98.0 + index + offset,
                100.0 + index + offset,
                1.0,
                index == 0,
            )
            for index in range(10)
        )
    for index in range(10):
        features = []
        for symbol_offset, symbol in enumerate(SYMBOLS):
            close = 100.0 + index + symbol_offset * 10.0
            vwap12 = close + 1.0 if index in (6, 7) else close - 1.0
            features.append(
                SymbolFeature(
                    symbol,
                    (index + 1) * H4,
                    0.01,
                    4.0,
                    2.0,
                    0.01,
                    vwap12,
                    close - 2.0,
                    50.0,
                    0.10,
                )
            )
        snapshots.append(
            CommonSnapshot((index + 1) * H4, -0.50, 0.0075, 2, 1, tuple(features))
        )
    context = s3.FeatureContext.from_h1(bars, tuple(snapshots))
    target = 9 * H4
    output = s3.generate_s3_global(context, s3.EmitWindow(target, target + H4), config)
    assert len(output.decisions) == 3
    assert len(output.accepted) == 1
    assert len(output.rejected) == 2
    assert [item.status for item in output.decisions].count("GENERATOR_ACCEPTED") == 1
    assert [item.status for item in output.decisions].count("GENERATOR_REJECTED") == 2
    assert output.accepted[0].identity not in {
        item.candidate.identity for item in output.rejected
    }
