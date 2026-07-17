"""ROB-945 (H5) -- per-scenario OOS ledger metrics + fold stability RED tests.

Each of the three independent cost scenarios (13/17/22bp) owns its own
ledger, trade count, and metrics -- this module must never revalue one
scenario's ledger to derive another's numbers, and the equal-weight,
exact-four-symbol pass-expectancy authority (Fable Q1=A,
orch-fable-answer-rob945b-20260718.md) must go ``incomplete`` rather than
inventing a value when any symbol has zero OOS trades.
"""

from __future__ import annotations

import math

import pytest
from rob940_engine import SignalEvent, TradeRecord
from rob945_scenario_metrics import (
    INSUFFICIENT_OOS_SYMBOL_EVIDENCE_REASON,
    NO_POSITIVE_MONTHS_REASON,
    compute_scenario_metrics,
)

_UNIVERSE = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")


def _trade(
    symbol,
    net_bps,
    *,
    strategy="S1",
    config_id="S1-00",
    fold_id="fold-00",
    signal_ts=1_000,
    entry_ts=2_000,
    exit_ts=3_000,
    exit_reason="take_profit",
):
    return TradeRecord(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        side="long",
        signal_ts=signal_ts,
        entry_ts=entry_ts,
        entry_price=100.0,
        exit_ts=exit_ts,
        exit_price=101.0,
        exit_reason=exit_reason,
        gross_bps=net_bps + 10.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=net_bps,
        fold_id=fold_id,
    )


def _signal(
    symbol,
    *,
    strategy="S1",
    config_id="S1-00",
    fold_id="fold-00",
    signal_ts=1_000,
    sl=200.0,
):
    return SignalEvent(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        signal_ts=signal_ts,
        side="long",
        sl_distance_bps=sl,
        tp_distance_bps=300.0,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id=fold_id,
    )


def _all_four_symbol_trades(net_bps_by_symbol):
    return [_trade(symbol, net_bps) for symbol, net_bps in net_bps_by_symbol.items()]


def _all_four_symbol_signals():
    return [_signal(symbol) for symbol in _UNIVERSE]


def test_all_four_symbols_have_trades_equal_weight_expectancy_is_mean_of_symbols():
    ledger = _all_four_symbol_trades(
        {"BTCUSDT": 10.0, "XRPUSDT": 20.0, "DOGEUSDT": 30.0, "SOLUSDT": 40.0}
    )
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=_all_four_symbol_signals(),
    )
    assert not result.incomplete
    # equal-weight mean of per-symbol expectancy, NOT pooled sum/total_trades
    # (identical here since one trade per symbol, but the field must be the
    # per-symbol MEAN, never silently substituted with the pooled value).
    assert result.net_expectancy_bps == pytest.approx(25.0)
    assert result.pooled_expectancy_bps == pytest.approx(25.0)


def test_one_zero_trade_symbol_makes_the_scenario_incomplete_not_a_fabricated_zero():
    ledger = _all_four_symbol_trades(
        {"BTCUSDT": 10.0, "XRPUSDT": 20.0, "DOGEUSDT": 30.0}
    )  # SOLUSDT has zero trades
    signals = [_signal(s) for s in ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=signals,
    )
    assert result.incomplete
    assert result.incomplete_reason == INSUFFICIENT_OOS_SYMBOL_EVIDENCE_REASON
    assert result.net_expectancy_bps is None
    sol = next(m for m in result.symbol_metrics if m.symbol == "SOLUSDT")
    assert sol.trade_count == 0
    assert sol.net_expectancy_bps is None
    assert sol.signal_count == 1  # a signal existed even though no trade resulted


def test_zero_trade_symbol_h6_attempt_status_distinction_is_caller_concern_not_this_module():
    """Zero trades is not an execution failure -- H6 attempt status/
    completeness is untouched by this incomplete flag; this module only
    ever reports H5 screen-evidence incompleteness."""
    ledger = _all_four_symbol_trades(
        {"BTCUSDT": 10.0, "XRPUSDT": 20.0, "DOGEUSDT": 30.0}
    )
    signals = [_signal(s) for s in _UNIVERSE]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=signals,
    )
    assert result.incomplete is True
    assert not hasattr(result, "h6_attempt_status")


def test_profit_factor_is_positive_infinity_when_all_trades_win():
    ledger = _all_four_symbol_trades(dict.fromkeys(_UNIVERSE, 10.0))
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=_all_four_symbol_signals(),
    )
    assert math.isinf(result.profit_factor) and result.profit_factor > 0


def test_profit_factor_is_nan_when_there_are_zero_trades():
    result = compute_scenario_metrics(
        strategy="S1", scenario_name="primary_stress", ledger=[], captured_signals=[]
    )
    assert math.isnan(result.profit_factor)


def test_win_rate_and_timeout_ratio():
    ledger = [
        _trade("BTCUSDT", 10.0, exit_reason="take_profit"),
        _trade(
            "BTCUSDT",
            -5.0,
            exit_reason="stop_loss",
            signal_ts=1_100,
            entry_ts=2_100,
            exit_ts=3_100,
        ),
        _trade(
            "BTCUSDT",
            0.0,
            exit_reason="timeout",
            signal_ts=1_200,
            entry_ts=2_200,
            exit_ts=3_200,
        ),
    ]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=[],
    )
    assert result.win_rate == pytest.approx(1 / 3)
    assert result.timeout_ratio == pytest.approx(1 / 3)


def test_mdd_in_r_uses_linked_signal_sl_distance_and_starts_curve_at_zero():
    trades = [
        _trade(
            "BTCUSDT", 100.0, signal_ts=1_000, entry_ts=2_000, exit_ts=3_000
        ),  # +0.5R (sl=200bps)
        _trade(
            "BTCUSDT", -300.0, signal_ts=1_100, entry_ts=2_100, exit_ts=3_100
        ),  # -1.5R
        _trade(
            "BTCUSDT", 50.0, signal_ts=1_200, entry_ts=2_200, exit_ts=3_200
        ),  # +0.25R
    ]
    signals = [
        _signal("BTCUSDT", signal_ts=1_000, sl=200.0),
        _signal("BTCUSDT", signal_ts=1_100, sl=200.0),
        _signal("BTCUSDT", signal_ts=1_200, sl=200.0),
    ]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=trades,
        captured_signals=signals,
    )
    # curve (starting at 0): 0 -> 0.5 -> -1.0 -> -0.75 ; peak 0.5, trough -1.0
    assert result.mdd_r == pytest.approx(1.5)
    assert result.mdd_reason is None


def test_mdd_is_unavailable_when_sl_evidence_is_missing():
    trades = [_trade("BTCUSDT", 100.0, signal_ts=999)]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=trades,
        captured_signals=[],
    )
    assert result.mdd_r is None
    assert result.mdd_reason == "mdd_unavailable_missing_sl_evidence"


def test_mdd_is_unavailable_when_signal_sl_evidence_is_ambiguous():
    """Two captured signals claiming the SAME frozen identity
    (strategy/config_id/symbol/fold_id/signal_ts) with DIFFERENT
    sl_distance_bps must never be silently resolved by picking one --
    real H4 evidence is deduplicated upstream (``OosSignalCaptureSink``),
    so this can only happen via a malformed/adversarial input to this
    pure function; it must still fail closed rather than pick a value."""
    trades = [_trade("BTCUSDT", 100.0, signal_ts=1_000)]
    signals = [
        _signal("BTCUSDT", signal_ts=1_000, sl=200.0),
        _signal("BTCUSDT", signal_ts=1_000, sl=400.0),
    ]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=trades,
        captured_signals=signals,
    )
    assert result.mdd_r is None
    assert result.mdd_reason == "mdd_unavailable_missing_sl_evidence"


def test_monthly_concentration_ignores_negative_and_zero_months_in_denominator():
    def _trade_in_month(symbol, net_bps, year, month, day):
        import datetime

        ts = int(
            datetime.datetime(year, month, day, tzinfo=datetime.UTC).timestamp() * 1000
        )
        return _trade(symbol, net_bps, signal_ts=ts, entry_ts=ts, exit_ts=ts)

    ledger = [
        _trade_in_month("BTCUSDT", 100.0, 2026, 1, 1),
        _trade_in_month("BTCUSDT", 300.0, 2026, 2, 1),
        _trade_in_month(
            "BTCUSDT", -500.0, 2026, 3, 1
        ),  # negative month excluded from denom
    ]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=[],
    )
    assert result.monthly_concentration == pytest.approx(300.0 / 400.0)
    assert result.monthly_concentration_reason is None


def test_no_positive_months_is_undefined_not_a_divide_by_total_pnl():
    def _trade_in_month(symbol, net_bps, year, month, day):
        import datetime

        ts = int(
            datetime.datetime(year, month, day, tzinfo=datetime.UTC).timestamp() * 1000
        )
        return _trade(symbol, net_bps, signal_ts=ts, entry_ts=ts, exit_ts=ts)

    ledger = [_trade_in_month("BTCUSDT", -100.0, 2026, 1, 1)]
    result = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger,
        captured_signals=[],
    )
    assert result.monthly_concentration is None
    assert result.monthly_concentration_reason == NO_POSITIVE_MONTHS_REASON


def test_two_scenarios_with_divergent_trade_counts_never_cross_contaminate():
    """3/3/2 divergence: @17 has 3 trades, @22 has only 2 (one extra
    daily-stop) -- each scenario's own metrics must derive ONLY from its own
    ledger, never a linear -5bp/trade revaluation of the other."""
    ledger_17 = _all_four_symbol_trades(dict.fromkeys(_UNIVERSE, 10.0))
    ledger_22 = _all_four_symbol_trades(
        {"BTCUSDT": 10.0, "XRPUSDT": 10.0, "DOGEUSDT": 10.0}
    )  # SOLUSDT daily-stopped out before entry at 22bp
    signals = _all_four_symbol_signals()
    result_17 = compute_scenario_metrics(
        strategy="S1",
        scenario_name="primary_stress",
        ledger=ledger_17,
        captured_signals=signals,
    )
    result_22 = compute_scenario_metrics(
        strategy="S1",
        scenario_name="upward_stress",
        ledger=ledger_22,
        captured_signals=signals,
    )
    assert result_17.trade_count == 4
    assert result_22.trade_count == 3
    assert result_22.incomplete  # SOLUSDT has zero trades at 22bp
    assert not result_17.incomplete


def test_wrong_strategy_trade_fails_closed():
    ledger = [_trade("BTCUSDT", 10.0, strategy="S2")]
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=ledger,
            captured_signals=[],
        )


def test_unknown_symbol_trade_fails_closed():
    ledger = [_trade("NOTACOIN", 10.0)]
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=ledger,
            captured_signals=[],
        )


def test_duplicate_trade_identity_fails_closed():
    trade = _trade("BTCUSDT", 10.0)
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[trade, trade],
            captured_signals=[],
        )


def test_non_finite_raw_trade_net_bps_fails_closed_before_any_sentinel_conversion():
    """A caller-supplied NaN/Inf economic value is a raw-input DEFECT, not a
    legitimate derived +Inf profit factor -- only THIS module's own PF
    computation may emit +Inf; raw trade input must be finite."""
    ledger = [_trade("BTCUSDT", float("nan"))]
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=ledger,
            captured_signals=[],
        )
    ledger_inf = [_trade("BTCUSDT", float("inf"))]
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=ledger_inf,
            captured_signals=[],
        )


def test_nonpositive_entry_price_fails_closed():
    bad = TradeRecord(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        side="long",
        signal_ts=1_000,
        entry_ts=2_000,
        entry_price=0.0,
        exit_ts=3_000,
        exit_price=101.0,
        exit_reason="take_profit",
        gross_bps=20.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=10.0,
        fold_id="fold-00",
    )
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[bad],
            captured_signals=[],
        )


def test_out_of_order_timestamps_fail_closed():
    bad = TradeRecord(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        side="long",
        signal_ts=5_000,
        entry_ts=2_000,  # entry BEFORE signal -- impossible, must fail closed
        entry_price=100.0,
        exit_ts=3_000,
        exit_price=101.0,
        exit_reason="take_profit",
        gross_bps=20.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=10.0,
        fold_id="fold-00",
    )
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[bad],
            captured_signals=[],
        )


def test_unknown_exit_reason_fails_closed():
    bad = TradeRecord(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        side="long",
        signal_ts=1_000,
        entry_ts=2_000,
        entry_price=100.0,
        exit_ts=3_000,
        exit_price=101.0,
        exit_reason="forged_exit_reason",
        gross_bps=20.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=10.0,
        fold_id="fold-00",
    )
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[bad],
            captured_signals=[],
        )


def test_unknown_side_fails_closed():
    bad = TradeRecord(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        side="sideways",
        signal_ts=1_000,
        entry_ts=2_000,
        entry_price=100.0,
        exit_ts=3_000,
        exit_price=101.0,
        exit_reason="take_profit",
        gross_bps=20.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=10.0,
        fold_id="fold-00",
    )
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[bad],
            captured_signals=[],
        )


def test_wrong_strategy_signal_fails_closed():
    signals = [_signal("BTCUSDT", strategy="S2")]
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[],
            captured_signals=signals,
        )


def test_unknown_symbol_signal_fails_closed():
    signals = [_signal("NOTACOIN")]
    with pytest.raises(ValueError):
        compute_scenario_metrics(
            strategy="S1",
            scenario_name="primary_stress",
            ledger=[],
            captured_signals=signals,
        )
