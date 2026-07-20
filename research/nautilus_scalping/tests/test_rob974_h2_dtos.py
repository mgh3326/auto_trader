"""ROB-979 (H2, ROB-974 R2) CP1 — immutable DTO + timestamp/type authority (RED first).

Covers ROB-979 AC1-4: immutable S3/S4 intent+trade+no-trade DTOs, exact
built-in int/float typing (reject bool/int-as-float/Decimal/subclasses), and
signal_ts causal-timestamp identity. See ``rob974_h2_dtos.py`` module docstring
for the ultrathink design log.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from rob974_h2_dtos import (
    UNIVERSE,
    MinuteBar,
    S3CloseFeature,
    S3NoTradeRecord,
    S3SignalIntent,
    S3Trade,
    S4NoTradeRecord,
    S4PairLegClose,
    S4PairSignalIntent,
    S4PairTrade,
)


def _minute_bar(**overrides):
    fields = {
        "symbol": "XRPUSDT",
        "open_time": 1_000_000,
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.05,
    }
    fields.update(overrides)
    return MinuteBar(**fields)


def _s3_intent(**overrides):
    fields = {
        "symbol": "XRPUSDT",
        "side": "long",
        "signal_ts": 1_000_000,
        "entry_sl_distance": 0.0080,
        "entry_tp_distance": 0.0128,
        "config_id": "s3-00",
        "fold_id": "fold-00",
        "volatility_percentile": 55.0,
    }
    fields.update(overrides)
    return S3SignalIntent(**fields)


def _s4_intent(**overrides):
    fields = {
        "pair": ("XRPUSDT", "DOGEUSDT"),
        "signal_ts": 1_000_000,
        "side_a": "short",
        "side_b": "long",
        "weight_a": 0.4,
        "weight_b": 0.6,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": 0.01,
        "sigma": 0.02,
        "z_entry": 1.9,
        "gross_notional": 15.0,
        "entry_sl_distance": 0.0100,
        "entry_tp_distance": 0.0150,
        "config_id": "s4-00",
        "fold_id": "fold-00",
    }
    fields.update(overrides)
    return S4PairSignalIntent(**fields)


def _s4_trade(**overrides):
    fields = {
        "pair": ("XRPUSDT", "DOGEUSDT"),
        "side_a": "short",
        "side_b": "long",
        "config_id": "s4-00",
        "fold_id": "fold-00",
        "signal_ts": 1_000_000,
        "entry_ts": 1_000_000,
        "weight_a": 0.4,
        "weight_b": 0.6,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": 0.0,
        "sigma": 0.05,
        "z_entry": 1.9,
        "gross_notional": 15.0,
        "entry_price_a": 1.0,
        "entry_price_b": 0.5,
        "exit_ts": 1_060_000,
        "exit_price_a": 0.99,
        "exit_price_b": 0.51,
        "exit_reason": "TP",
        "mfe_bps": 100.0,
        "mae_bps": -10.0,
        "gross_bps": 100.0,
        "order_id_a": None,
        "order_id_b": None,
        "pair_exec_status": "historical_atomic_assumption",
        "pair_executor_validated": False,
        "demo_eligible": False,
        "volatility_percentile": None,
        "volatility_percentile_provenance": "not_defined_for_s4",
    }
    fields.update(overrides)
    return S4PairTrade(**fields)


class TestMinuteBarTyping:
    def test_accepts_exact_types(self):
        bar = _minute_bar()
        assert bar.open_time == 1_000_000
        assert type(bar.open) is float

    def test_frozen_immutable(self):
        bar = _minute_bar()
        with pytest.raises(FrozenInstanceError):
            bar.open = 2.0

    def test_rejects_bool_open_time(self):
        with pytest.raises(TypeError):
            _minute_bar(open_time=True)

    def test_rejects_int_price_masquerading_as_float(self):
        with pytest.raises(TypeError):
            _minute_bar(open=1)

    def test_rejects_bool_price(self):
        with pytest.raises(TypeError):
            _minute_bar(open=True)

    def test_rejects_decimal_price(self):
        with pytest.raises(TypeError):
            _minute_bar(open=Decimal("1.0"))

    def test_rejects_nonfinite_price(self):
        with pytest.raises(ValueError):
            _minute_bar(open=float("nan"))

    def test_rejects_unknown_symbol(self):
        with pytest.raises(ValueError):
            _minute_bar(symbol="BTCUSDT")


class TestS3CloseFeature:
    def test_exact_types_and_frozen(self):
        feat = S3CloseFeature(
            symbol="DOGEUSDT", close_ts=2_000_000, close=0.5, vwap24=0.49, m=0.0081
        )
        assert type(feat.close_ts) is int
        with pytest.raises(FrozenInstanceError):
            feat.close = 0.6

    def test_rejects_bool_close_ts(self):
        with pytest.raises(TypeError):
            S3CloseFeature(
                symbol="DOGEUSDT", close_ts=True, close=0.5, vwap24=0.49, m=0.0081
            )


class TestS4PairLegClose:
    def test_exact_types(self):
        leg = S4PairLegClose(symbol="SOLUSDT", close_ts=2_000_000, close=100.0)
        assert type(leg.close) is float

    def test_rejects_decimal_close(self):
        with pytest.raises(TypeError):
            S4PairLegClose(symbol="SOLUSDT", close_ts=2_000_000, close=Decimal("100"))


class TestS3SignalIntent:
    def test_valid_construction(self):
        intent = _s3_intent()
        assert intent.symbol in UNIVERSE
        assert intent.side == "long"

    def test_frozen(self):
        intent = _s3_intent()
        with pytest.raises(FrozenInstanceError):
            intent.side = "short"

    def test_rejects_unknown_side(self):
        with pytest.raises(ValueError):
            _s3_intent(side="up")

    def test_rejects_nonpositive_sl_distance(self):
        with pytest.raises(ValueError):
            _s3_intent(entry_sl_distance=0.0)

    def test_rejects_bool_signal_ts(self):
        with pytest.raises(TypeError):
            _s3_intent(signal_ts=True)

    def test_rejects_int_distance(self):
        with pytest.raises(TypeError):
            _s3_intent(entry_sl_distance=1)


class TestS4PairSignalIntent:
    def test_valid_construction(self):
        intent = _s4_intent()
        assert intent.pair == ("XRPUSDT", "DOGEUSDT")

    def test_rejects_same_symbol_pair(self):
        with pytest.raises(ValueError):
            _s4_intent(pair=("XRPUSDT", "XRPUSDT"))

    def test_rejects_unordered_pair_not_in_universe(self):
        with pytest.raises(ValueError):
            _s4_intent(pair=("XRPUSDT", "BTCUSDT"))

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError):
            _s4_intent(weight_a=0.5, weight_b=0.6)

    def test_rejects_bool_weight(self):
        with pytest.raises(TypeError):
            _s4_intent(weight_a=True)

    def test_frozen(self):
        intent = _s4_intent()
        with pytest.raises(FrozenInstanceError):
            intent.weight_a = 0.5

    def test_rejects_out_of_range_beta(self):
        # verify-R1 finding 4 exact repro: beta_a=-999.0 was wrongly accepted.
        with pytest.raises(ValueError):
            _s4_intent(beta_a=-999.0)
        with pytest.raises(ValueError):
            _s4_intent(beta_b=99.0)

    def test_accepts_beta_at_the_clip_boundaries(self):
        assert _s4_intent(beta_a=0.25, beta_b=3.00).beta_a == 0.25

    def test_rejects_zero_z_entry(self):
        # verify-R1 finding 4 exact repro: z_entry=0.0 was wrongly accepted.
        with pytest.raises(ValueError):
            _s4_intent(z_entry=0.0)

    def test_rejects_degenerate_small_z_entry_magnitude(self):
        with pytest.raises(ValueError):
            _s4_intent(z_entry=0.5)
        with pytest.raises(ValueError):
            _s4_intent(z_entry=-0.5)


class TestS3Trade:
    def test_valid_construction_and_frozen(self):
        trade = S3Trade(
            symbol="XRPUSDT",
            side="long",
            config_id="s3-00",
            fold_id="fold-00",
            signal_ts=1_000_000,
            entry_ts=1_000_000,
            entry_price=1.0,
            exit_ts=1_060_000,
            exit_price=1.01,
            exit_reason="TP",
            mfe_bps=100.0,
            mae_bps=-10.0,
            gross_bps=100.0,
            volatility_percentile=55.0,
        )
        with pytest.raises(FrozenInstanceError):
            trade.exit_price = 2.0

    def test_rejects_bad_exit_reason(self):
        with pytest.raises(ValueError):
            S3Trade(
                symbol="XRPUSDT",
                side="long",
                config_id="s3-00",
                fold_id=None,
                signal_ts=1_000_000,
                entry_ts=1_000_000,
                entry_price=1.0,
                exit_ts=1_060_000,
                exit_price=1.01,
                exit_reason="thesis_exit",  # must be exact "THESIS_EXIT"
                mfe_bps=100.0,
                mae_bps=-10.0,
                gross_bps=100.0,
                volatility_percentile=None,
            )


class TestS4PairTrade:
    def test_single_record_carries_both_legs(self):
        trade = S4PairTrade(
            pair=("XRPUSDT", "DOGEUSDT"),
            side_a="short",
            side_b="long",
            config_id="s4-00",
            fold_id="fold-00",
            signal_ts=1_000_000,
            entry_ts=1_000_000,
            weight_a=0.4,
            weight_b=0.6,
            beta_a=1.2,
            beta_b=0.8,
            mu=0.0,
            sigma=0.05,
            z_entry=1.9,
            gross_notional=15.0,
            entry_price_a=1.0,
            entry_price_b=0.5,
            exit_ts=1_060_000,
            exit_price_a=0.99,
            exit_price_b=0.51,
            exit_reason="TP",
            mfe_bps=100.0,
            mae_bps=-10.0,
            gross_bps=100.0,
            order_id_a=None,
            order_id_b=None,
            pair_exec_status="historical_atomic_assumption",
            pair_executor_validated=False,
            demo_eligible=False,
            volatility_percentile=None,
            volatility_percentile_provenance="not_defined_for_s4",
        )
        # AC1: not representable as two independent single-leg records --
        # both legs live on the SAME frozen object.
        assert trade.entry_price_a == 1.0
        assert trade.entry_price_b == 0.5
        assert trade.pair_executor_validated is False
        assert trade.demo_eligible is False

    def test_rejects_order_id_present_historically(self):
        with pytest.raises(ValueError):
            S4PairTrade(
                pair=("XRPUSDT", "DOGEUSDT"),
                side_a="short",
                side_b="long",
                config_id="s4-00",
                fold_id=None,
                signal_ts=1_000_000,
                entry_ts=1_000_000,
                weight_a=0.4,
                weight_b=0.6,
                beta_a=1.2,
                beta_b=0.8,
                mu=0.0,
                sigma=0.05,
                z_entry=1.9,
                gross_notional=15.0,
                entry_price_a=1.0,
                entry_price_b=0.5,
                exit_ts=1_060_000,
                exit_price_a=0.99,
                exit_price_b=0.51,
                exit_reason="TP",
                mfe_bps=100.0,
                mae_bps=-10.0,
                gross_bps=100.0,
                order_id_a="not-null-should-fail",
                order_id_b=None,
                pair_exec_status="historical_atomic_assumption",
                pair_executor_validated=False,
                demo_eligible=False,
                volatility_percentile=None,
                volatility_percentile_provenance="not_defined_for_s4",
            )

    def test_rejects_demo_eligible_true(self):
        with pytest.raises(ValueError):
            S4PairTrade(
                pair=("XRPUSDT", "DOGEUSDT"),
                side_a="short",
                side_b="long",
                config_id="s4-00",
                fold_id=None,
                signal_ts=1_000_000,
                entry_ts=1_000_000,
                weight_a=0.4,
                weight_b=0.6,
                beta_a=1.2,
                beta_b=0.8,
                mu=0.0,
                sigma=0.05,
                z_entry=1.9,
                gross_notional=15.0,
                entry_price_a=1.0,
                entry_price_b=0.5,
                exit_ts=1_060_000,
                exit_price_a=0.99,
                exit_price_b=0.51,
                exit_reason="TP",
                mfe_bps=100.0,
                mae_bps=-10.0,
                gross_bps=100.0,
                order_id_a=None,
                order_id_b=None,
                pair_exec_status="historical_atomic_assumption",
                pair_executor_validated=False,
                demo_eligible=True,
                volatility_percentile=None,
                volatility_percentile_provenance="not_defined_for_s4",
            )

    def test_carries_entry_frozen_provenance(self):
        trade = _s4_trade(beta_a=1.2, beta_b=0.8, mu=0.01, sigma=0.05, z_entry=1.9)
        assert trade.beta_a == 1.2
        assert trade.beta_b == 0.8
        assert trade.mu == 0.01
        assert trade.sigma == 0.05
        assert trade.z_entry == 1.9
        assert trade.gross_notional == 15.0

    def test_rejects_out_of_range_beta(self):
        # verify-R1 finding 4 exact repro: beta_a=-999.0 was wrongly accepted.
        with pytest.raises(ValueError):
            _s4_trade(beta_a=-999.0)
        with pytest.raises(ValueError):
            _s4_trade(beta_b=99.0)

    def test_rejects_zero_z_entry(self):
        with pytest.raises(ValueError):
            _s4_trade(z_entry=0.0)

    def test_pair_exec_fail_defaults_to_not_evaluated_and_rejects_other_values(self):
        trade = _s4_trade()
        assert trade.pair_exec_fail == "not_evaluated"
        with pytest.raises(ValueError):
            _s4_trade(pair_exec_fail="pass")
        with pytest.raises(ValueError):
            _s4_trade(pair_exec_fail="0")

    def test_promotion_status_defaults_blocked_and_rejects_other_values(self):
        trade = _s4_trade()
        assert trade.promotion_status == "promotion_blocked_pending_pair_executor"
        with pytest.raises(ValueError):
            _s4_trade(promotion_status="promotion_ready")


class TestNoTradeRecords:
    def test_s3_no_trade_record(self):
        rec = S3NoTradeRecord(
            symbol="XRPUSDT",
            side="long",
            config_id="s3-00",
            fold_id=None,
            signal_ts=1_000_000,
            reason="next_tick_unavailable",
        )
        with pytest.raises(FrozenInstanceError):
            rec.reason = "other"

    def test_s4_no_trade_record(self):
        rec = S4NoTradeRecord(
            pair=("XRPUSDT", "DOGEUSDT"),
            config_id="s4-00",
            fold_id=None,
            signal_ts=1_000_000,
            reason="next_tick_unavailable",
        )
        assert rec.pair == ("XRPUSDT", "DOGEUSDT")
