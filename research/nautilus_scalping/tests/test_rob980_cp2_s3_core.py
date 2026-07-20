from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import math

import pytest
from rob974_features import Bar4h, CommonSnapshot, SymbolFeature
from rob974_h3_manifest import SYMBOLS, get_config

H4 = 4 * 60 * 60 * 1000


@pytest.fixture(scope="module")
def s3():
    spec = importlib.util.find_spec("rob974_h3_s3")
    assert spec is not None, "ROB-980 CP2 S3 formula core is not implemented"
    return importlib.import_module("rob974_h3_s3")


def _bars(count: int, *, start: int = 0, offset: float = 0.0):
    return tuple(
        Bar4h(
            index * H4,
            (index + 1) * H4,
            99.5 + index + offset,
            101.0 + index + offset,
            99.0 + index + offset,
            100.0 + index + offset,
            1.0,
            index == start,
        )
        for index in range(start, count)
    )


def _feature(
    symbol: str,
    ts: int,
    *,
    close: float,
    atr20: float | None = 2.0,
    a: float | None = 0.02,
    vwap12: float | None = None,
    vwap24: float | None = 98.0,
    percentile: float | None = 50.0,
    range24: float | None = 0.10,
):
    return SymbolFeature(
        symbol,
        ts,
        0.01,
        2.0,
        atr20,
        a,
        close if vwap12 is None else vwap12,
        vwap24,
        percentile,
        range24,
    )


def _snapshot(
    index: int,
    *,
    market_4h: float = -0.123,
    market_24h: float = 0.008,
    overrides: dict[str, dict[str, float | None]] | None = None,
):
    ts = (index + 1) * H4
    overrides = overrides or {}
    features = []
    for symbol_offset, symbol in enumerate(SYMBOLS):
        close = 100.0 + index + symbol_offset * 10.0
        features.append(_feature(symbol, ts, close=close, **overrides.get(symbol, {})))
    return CommonSnapshot(ts, market_4h, market_24h, 2, 1, tuple(features))


def _context(s3, count: int = 14, snapshots=None):
    bars = {
        symbol: _bars(count, offset=float(position * 10))
        for position, symbol in enumerate(SYMBOLS)
    }
    values = (
        tuple(_snapshot(index) for index in range(count))
        if snapshots is None
        else tuple(snapshots)
    )
    return s3.FeatureContext.from_h1(bars, values)


def test_feature_context_keeps_actual_h1_dtos_and_distinct_emit_window(s3):
    bars = {symbol: _bars(10) for symbol in SYMBOLS}
    snapshots = tuple(_snapshot(index) for index in range(10))
    context = s3.FeatureContext.from_h1(bars, snapshots)
    assert context.bars_for("XRPUSDT") is bars["XRPUSDT"]
    assert context.snapshots == snapshots
    assert all(type(bar) is Bar4h for bar in context.bars_for("XRPUSDT"))
    assert all(type(item) is CommonSnapshot for item in context.snapshots)
    window = s3.EmitWindow(3 * 60 * 60 * 1000, 13 * 60 * 60 * 1000)
    assert window not in dataclasses.astuple(context)
    assert s3.expected_decision_closes(window) == (H4, 2 * H4, 3 * H4)


def test_exact_s3_formula_uses_l_deltas_l_plus_one_closes_and_prior_q_only(s3):
    config = get_config("S3-01")  # L=8
    snapshots = [_snapshot(index) for index in range(12)]
    target_index = 8
    target_ts = (target_index + 1) * H4
    snapshots[target_index - 2] = _snapshot(
        target_index - 2,
        overrides={"XRPUSDT": {"vwap12": 107.0, "atr20": 2.0}},
    )
    snapshots[target_index - 1] = _snapshot(
        target_index - 1,
        overrides={"XRPUSDT": {"vwap12": 107.5, "atr20": 2.0}},
    )
    snapshots[target_index] = _snapshot(
        target_index,
        market_4h=-0.123,
        market_24h=0.008,
        overrides={
            "XRPUSDT": {
                "atr20": 9.0,
                "a": 0.02,
                "vwap12": 107.0,
                "vwap24": 106.0,
                "percentile": 20.0,
                "range24": 0.05,
            }
        },
    )
    context = _context(s3, snapshots=snapshots)
    metrics = s3.compute_s3_metrics(context, config, target_ts, "XRPUSDT")
    assert metrics is not None
    expected_r = math.log(108.0 / 100.0)
    assert metrics.R == expected_r
    assert metrics.ER == 1.0
    assert metrics.S == expected_r / (0.02 * math.sqrt(8))
    assert metrics.Qplus == 0.5
    assert metrics.Qminus == -0.25
    assert metrics.atr20 == 9.0
    assert metrics.A == 0.02
    assert metrics.vwap24 == 106.0
    assert metrics.percentile_30d == 20.0
    assert metrics.range24 == 0.05
    assert metrics.market_return_24h == 0.008
    assert metrics.current_market_return_4h == -0.123
    assert metrics.decision_ts == target_ts

    changed = list(snapshots)
    changed[target_index] = _snapshot(
        target_index,
        overrides={"XRPUSDT": {"vwap12": 1.0, "atr20": 9.0, "a": 0.02}},
    )
    changed_metrics = s3.compute_s3_metrics(
        _context(s3, snapshots=changed), config, target_ts, "XRPUSDT"
    )
    assert changed_metrics is not None
    assert (changed_metrics.Qplus, changed_metrics.Qminus) == (
        metrics.Qplus,
        metrics.Qminus,
    )


def test_expected_grid_counts_missing_middle_and_recovers_without_state(s3):
    config = get_config("S3-01")
    snapshots = [_snapshot(index) for index in range(16)]
    missing_ts = 10 * H4
    snapshots = [item for item in snapshots if item.decision_ts != missing_ts]
    context = _context(s3, count=16, snapshots=snapshots)
    window = s3.EmitWindow(9 * H4, 14 * H4)
    units = s3.s3_formula_grid(context, window, config)
    assert len(units) == 5 * 3
    assert tuple(sorted({unit.decision_ts for unit in units})) == tuple(
        range(9 * H4, 14 * H4, H4)
    )
    missing = [unit for unit in units if unit.decision_ts == missing_ts]
    assert len(missing) == 3
    assert all(unit.metrics is None for unit in missing)
    recovered = [
        unit
        for unit in units
        if unit.decision_ts == 13 * H4 and unit.symbol == "XRPUSDT"
    ]
    assert len(recovered) == 1
    assert recovered[0].metrics is not None


def test_missing_required_h1_values_are_no_signal(s3):
    config = get_config("S3-01")
    snapshots = [_snapshot(index) for index in range(12)]
    target_index = 9
    snapshots[target_index] = _snapshot(
        target_index,
        overrides={"XRPUSDT": {"range24": None}},
    )
    context = _context(s3, snapshots=snapshots)
    assert (
        s3.compute_s3_metrics(context, config, (target_index + 1) * H4, "XRPUSDT")
        is None
    )


def test_future_bar_mutation_cannot_change_prior_decision(s3):
    config = get_config("S3-01")
    snapshots = tuple(_snapshot(index) for index in range(14))
    bars = {
        symbol: _bars(14, offset=float(position * 10))
        for position, symbol in enumerate(SYMBOLS)
    }
    decision_ts = 10 * H4
    baseline = s3.compute_s3_metrics(
        s3.FeatureContext.from_h1(bars, snapshots),
        config,
        decision_ts,
        "XRPUSDT",
    )
    changed = dict(bars)
    future = list(changed["XRPUSDT"])
    future[11] = dataclasses.replace(
        future[11], open=500.0, high=900.0, low=400.0, close=800.0
    )
    changed["XRPUSDT"] = tuple(future)
    assert (
        s3.compute_s3_metrics(
            s3.FeatureContext.from_h1(changed, snapshots),
            config,
            decision_ts,
            "XRPUSDT",
        )
        == baseline
    )


def test_bar_gap_cannot_bridge_and_new_segment_later_recovers(s3):
    config = get_config("S3-01")
    source = list(_bars(22))
    source[10] = dataclasses.replace(source[10], is_segment_start=True)
    bars = {
        "XRPUSDT": tuple(source),
        "DOGEUSDT": _bars(22, offset=10.0),
        "SOLUSDT": _bars(22, offset=20.0),
    }
    snapshots = tuple(_snapshot(index) for index in range(22))
    context = s3.FeatureContext.from_h1(bars, snapshots)
    assert s3.compute_s3_metrics(context, config, 11 * H4, "XRPUSDT") is None
    assert s3.compute_s3_metrics(context, config, 19 * H4, "XRPUSDT") is not None


def test_exact_timestamp_and_malformed_nonfinite_are_terminal(s3):
    with pytest.raises(TypeError):
        s3.EmitWindow(True, H4)
    context = _context(s3)
    with pytest.raises(TypeError):
        s3.compute_s3_metrics(context, get_config("S3-01"), True, "XRPUSDT")

    corrupted = _snapshot(10)
    object.__setattr__(corrupted, "M", math.nan)
    with pytest.raises(ValueError):
        s3.FeatureContext.from_h1(
            {symbol: _bars(14) for symbol in SYMBOLS}, (corrupted,)
        )
