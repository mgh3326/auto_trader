"""ROB-982 CP7 -- H4 independent full-window PBO grid + evaluator seam (RED first).

Covers: 24-config fan-out with exactly one fresh generator call and one
actual-H2-engine call per config (never selected-OOS reuse), fold_id=None
full-window aggregation of raw trades into a zero-filled 365-day UTC-exit-day
grid, a closed-shape ``H4PboEvidence`` fed only from the frozen
``probability_backtest_overfitting`` CSCV primitive, and fail-closed
rejection of malformed grids (wrong config/day count, non-numeric/non-finite
cells, fold-scoped trades, aliased generator buffers).
"""

from __future__ import annotations

import math
from collections import namedtuple
from decimal import Decimal

import pytest
from rob945_pbo_grid import FROZEN_DAY_KEYS
from rob974_h2_dtos import MinuteBar, S3SignalIntent, S4PairSignalIntent
from rob974_h2_ingress import build_minute_index
from rob974_h4_contracts import WINDOW_END_MS, WINDOW_START_MS
from rob974_h4_pbo import (
    H4PboError,
    H4PboEvidence,
    aggregate_full_window_s3_trades,
    compute_h4_full_window_pbo,
    run_full_window_s3_configs,
    run_full_window_s4_configs,
    seal_full_window_grid,
    validate_full_window_grid,
)

_MIN_MS = 60_000
_Cfg = namedtuple("Cfg", ["config_id"])


def _s3_configs(strategy="S3", count=24):
    return tuple(_Cfg(f"{strategy}-{i:02d}") for i in range(count))


def _bars(symbol, start_ts, count, price=1.0, overrides=None):
    overrides = overrides or {}
    out = []
    for i in range(count):
        ts = start_ts + i * _MIN_MS
        o, h, low, c = overrides.get(i, (price, price, price, price))
        out.append(MinuteBar(symbol, ts, o, h, low, c))
    return out


def _s3_intent(symbol="XRPUSDT", side="long", signal_ts=WINDOW_START_MS, **kw):
    fields = {
        "symbol": symbol,
        "side": side,
        "signal_ts": signal_ts,
        "entry_sl_distance": 0.0080,
        "entry_tp_distance": 0.0128,
        "config_id": "S3-00",
        "fold_id": None,
        "volatility_percentile": 55.0,
    }
    fields.update(kw)
    return S3SignalIntent(**fields)


def _s4_intent(pair=("XRPUSDT", "DOGEUSDT"), signal_ts=WINDOW_START_MS, **kw):
    fields = {
        "pair": pair,
        "signal_ts": signal_ts,
        "side_a": "short",
        "side_b": "long",
        "weight_a": 0.4,
        "weight_b": 0.6,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": 0.0,
        "sigma": 0.05,
        "z_entry": 1.9,
        "gross_notional": max(6 / 0.4, 6 / 0.6),
        "entry_sl_distance": 0.0100,
        "entry_tp_distance": 0.0150,
        "config_id": "S4-00",
        "fold_id": None,
    }
    fields.update(kw)
    return S4PairSignalIntent(**fields)


def _single_sl_generator_and_index():
    """One candidate on config-00 only, resolving via an immediate gap SL;
    every other config gets zero candidates (empty grid rows)."""
    bars = _bars("XRPUSDT", WINDOW_START_MS, 3, price=1.0)
    bars[1] = MinuteBar("XRPUSDT", WINDOW_START_MS + _MIN_MS, 0.90, 0.90, 0.90, 0.90)
    minute_index = build_minute_index(bars)

    calls: list[str] = []

    def generator(config):
        calls.append(config.config_id)
        if config.config_id == "S3-00":
            return [_s3_intent(signal_ts=WINDOW_START_MS)]
        return []

    return generator, calls, minute_index


class TestFullWindowFanOut:
    def test_exactly_24_generator_calls_and_grid_rows(self):
        generator, calls, minute_index = _single_sl_generator_and_index()
        grid = run_full_window_s3_configs(
            configs=_s3_configs("S3"),
            generator=generator,
            minute_index=minute_index,
            close_feature_index={},
        )
        assert len(calls) == 24
        assert len(calls) == len(set(calls))
        assert set(grid.keys()) == {f"S3-{i:02d}" for i in range(24)}

    def test_every_row_has_exactly_the_frozen_365_day_keys(self):
        generator, _calls, minute_index = _single_sl_generator_and_index()
        grid = run_full_window_s3_configs(
            configs=_s3_configs("S3"),
            generator=generator,
            minute_index=minute_index,
            close_feature_index={},
        )
        for config_id, row in grid.items():
            assert tuple(row.keys()) == FROZEN_DAY_KEYS, config_id
            assert len(row) == 365

    def test_config_with_trade_is_nonzero_others_are_zero(self):
        generator, _calls, minute_index = _single_sl_generator_and_index()
        grid = run_full_window_s3_configs(
            configs=_s3_configs("S3"),
            generator=generator,
            minute_index=minute_index,
            close_feature_index={},
        )
        entry_day = FROZEN_DAY_KEYS[0]
        assert grid["S3-00"][entry_day] != 0.0
        assert grid["S3-01"][entry_day] == 0.0
        assert all(v == 0.0 for v in grid["S3-01"].values())

    def test_shared_candidate_buffer_across_configs_is_rejected(self):
        bars = _bars("XRPUSDT", WINDOW_START_MS, 3, price=1.0)
        minute_index = build_minute_index(bars)
        shared: list = []

        def generator(_config):
            return shared

        with pytest.raises(H4PboError):
            run_full_window_s3_configs(
                configs=_s3_configs("S3"),
                generator=generator,
                minute_index=minute_index,
                close_feature_index={},
            )

    def test_wrong_config_count_is_rejected(self):
        generator, _calls, minute_index = _single_sl_generator_and_index()
        with pytest.raises(H4PboError):
            run_full_window_s3_configs(
                configs=_s3_configs("S3", count=23),
                generator=generator,
                minute_index=minute_index,
                close_feature_index={},
            )

    def test_fold_scoped_trade_is_rejected(self):
        bars = _bars("XRPUSDT", WINDOW_START_MS, 3, price=1.0)
        bars[1] = MinuteBar(
            "XRPUSDT", WINDOW_START_MS + _MIN_MS, 0.90, 0.90, 0.90, 0.90
        )
        minute_index = build_minute_index(bars)

        def generator(config):
            if config.config_id == "S3-00":
                return [_s3_intent(signal_ts=WINDOW_START_MS, fold_id="fold-00")]
            return []

        with pytest.raises(H4PboError):
            run_full_window_s3_configs(
                configs=_s3_configs("S3"),
                generator=generator,
                minute_index=minute_index,
                close_feature_index={},
            )


class TestS4FullWindowFanOut:
    def test_exactly_24_generator_calls_and_grid_rows(self):
        bars_a = _bars("XRPUSDT", WINDOW_START_MS, 3, price=1.0)
        bars_a[1] = MinuteBar(
            "XRPUSDT", WINDOW_START_MS + _MIN_MS, 1.20, 1.20, 1.20, 1.20
        )
        bars_b = _bars("DOGEUSDT", WINDOW_START_MS, 3, price=1.0)
        bars_b[1] = MinuteBar(
            "DOGEUSDT", WINDOW_START_MS + _MIN_MS, 1.20, 1.20, 1.20, 1.20
        )
        minute_index = build_minute_index(bars_a + bars_b)
        calls: list[str] = []

        def generator(config):
            calls.append(config.config_id)
            if config.config_id == "S4-00":
                return [_s4_intent(signal_ts=WINDOW_START_MS)]
            return []

        grid = run_full_window_s4_configs(
            configs=_s3_configs("S4"),
            generator=generator,
            minute_index=minute_index,
            pair_close_index={},
        )
        assert len(calls) == 24
        assert set(grid.keys()) == {f"S4-{i:02d}" for i in range(24)}


class TestGridValidation:
    def test_valid_grid_round_trips(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        aligned = validate_full_window_grid(
            strategy="S3", daily_gross_bps_by_config=grid
        )
        assert set(aligned.keys()) == set(grid.keys())
        assert all(len(v) == 365 for v in aligned.values())

    def test_23_configs_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(23)}
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_25_configs_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        grid["S3-24"] = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_364_days_rejected(self):
        grid = {
            f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS[:-1], 0.0) for i in range(24)
        }
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_366_days_rejected(self):
        grid = {
            f"S3-{i:02d}": {**dict.fromkeys(FROZEN_DAY_KEYS, 0.0), "2099-01-01": 0.0}
            for i in range(24)
        }
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_bool_cell_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        grid["S3-00"] = dict(grid["S3-00"])
        grid["S3-00"][FROZEN_DAY_KEYS[0]] = True
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_decimal_cell_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        grid["S3-00"] = dict(grid["S3-00"])
        grid["S3-00"][FROZEN_DAY_KEYS[0]] = Decimal("1.0")
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_float_subclass_cell_rejected(self):
        class _FloatSubclass(float):
            pass

        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        grid["S3-00"] = dict(grid["S3-00"])
        grid["S3-00"][FROZEN_DAY_KEYS[0]] = _FloatSubclass(1.0)
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_nan_cell_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        grid["S3-00"] = dict(grid["S3-00"])
        grid["S3-00"][FROZEN_DAY_KEYS[0]] = math.nan
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_inf_cell_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        grid["S3-00"] = dict(grid["S3-00"])
        grid["S3-00"][FROZEN_DAY_KEYS[0]] = math.inf
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S3", daily_gross_bps_by_config=grid)

    def test_wrong_strategy_rejected(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        with pytest.raises(H4PboError):
            validate_full_window_grid(strategy="S1", daily_gross_bps_by_config=grid)


class TestExitDayOutOfWindow:
    def test_trade_exit_beyond_window_is_rejected(self):
        from rob974_h2_dtos import S3Trade

        bogus_trade = S3Trade(
            symbol="XRPUSDT",
            side="long",
            config_id="S3-00",
            fold_id=None,
            signal_ts=WINDOW_START_MS,
            entry_ts=WINDOW_START_MS,
            entry_price=1.0,
            exit_ts=WINDOW_END_MS + _MIN_MS,
            exit_price=1.0128,
            exit_reason="TP",
            mfe_bps=128.0,
            mae_bps=0.0,
            gross_bps=127.3,
        )
        day_totals = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)
        with pytest.raises(H4PboError):
            aggregate_full_window_s3_trades(
                "S3-00", (bogus_trade,), day_totals=day_totals
            )


class TestSealAndEvidence:
    def test_seal_is_deterministic_and_order_independent(self):
        grid_a = {
            f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, float(i)) for i in range(24)
        }
        grid_b = dict(reversed(grid_a.items()))
        aligned_a = validate_full_window_grid(
            strategy="S3", daily_gross_bps_by_config=grid_a
        )
        aligned_b = validate_full_window_grid(
            strategy="S3", daily_gross_bps_by_config=grid_b
        )
        seal_a = seal_full_window_grid(strategy="S3", aligned=aligned_a)
        seal_b = seal_full_window_grid(strategy="S3", aligned=aligned_b)
        assert seal_a == seal_b

    def test_seal_changes_when_a_cell_changes(self):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        aligned_zero = validate_full_window_grid(
            strategy="S3", daily_gross_bps_by_config=grid
        )
        grid["S3-00"] = dict(grid["S3-00"])
        grid["S3-00"][FROZEN_DAY_KEYS[0]] = 1.0
        aligned_one = validate_full_window_grid(
            strategy="S3", daily_gross_bps_by_config=grid
        )
        assert seal_full_window_grid(
            strategy="S3", aligned=aligned_zero
        ) != seal_full_window_grid(strategy="S3", aligned=aligned_one)

    def test_compute_h4_full_window_pbo_returns_closed_evidence_shape(self):
        grid = {f"S4-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        evidence = compute_h4_full_window_pbo(
            strategy="S4", daily_gross_bps_by_config=grid
        )
        assert type(evidence) is H4PboEvidence
        assert evidence.strategy == "S4"
        assert evidence.slices == 4
        assert evidence.config_count == 24
        assert evidence.day_count == 365
        assert (
            isinstance(evidence.grid_seal_sha256, str)
            and len(evidence.grid_seal_sha256) == 64
        )
        field_names = {f.name for f in __import__("dataclasses").fields(evidence)}
        assert field_names == {
            "strategy",
            "value",
            "reason_codes",
            "slices",
            "config_count",
            "day_count",
            "grid_seal_sha256",
        }

    def test_compute_h4_full_window_pbo_is_reference_only_never_raises_on_all_zero_grid(
        self,
    ):
        grid = {f"S3-{i:02d}": dict.fromkeys(FROZEN_DAY_KEYS, 0.0) for i in range(24)}
        evidence = compute_h4_full_window_pbo(
            strategy="S3", daily_gross_bps_by_config=grid
        )
        assert evidence.value is None or isinstance(evidence.value, float)
        assert isinstance(evidence.reason_codes, tuple)
