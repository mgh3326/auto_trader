"""ROB-945 (H5) -- fold stability RED tests (positive OOS fold count is a
hard pass criterion #3: @17 positive OOS folds >= 4)."""

from __future__ import annotations

import math

import pytest
from rob940_engine import TradeRecord
from rob945_scenario_metrics import compute_fold_stability


def _trade(symbol, net_bps, fold_id, config_id="S1-00"):
    return TradeRecord(
        strategy="S1",
        config_id=config_id,
        symbol=symbol,
        side="long",
        signal_ts=1_000,
        entry_ts=2_000,
        entry_price=100.0,
        exit_ts=3_000,
        exit_price=101.0,
        exit_reason="take_profit",
        gross_bps=net_bps + 10.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=net_bps,
        fold_id=fold_id,
    )


def test_fold_with_positive_net_pnl_is_positive():
    ledger = [_trade("BTCUSDT", 50.0, "fold-00"), _trade("XRPUSDT", 20.0, "fold-00")]
    rows = compute_fold_stability(
        ledger=ledger, fold_selected_config={"fold-00": "S1-03"}
    )
    row = rows[0]
    assert row.fold_id == "fold-00"
    assert row.selected_config_id == "S1-03"
    assert row.trade_count == 2
    assert row.net_pnl_bps == pytest.approx(70.0)
    assert row.positive is True
    assert row.net_pnl_class == "positive"
    assert math.isinf(row.profit_factor) and row.profit_factor > 0  # no losses


def test_fold_profit_factor_is_a_ratio_of_gross_profit_to_gross_loss():
    ledger = [
        _trade("BTCUSDT", 30.0, "fold-00"),
        _trade("XRPUSDT", -10.0, "fold-00"),
    ]
    rows = compute_fold_stability(
        ledger=ledger, fold_selected_config={"fold-00": "S1-03"}
    )
    assert rows[0].profit_factor == pytest.approx(3.0)


def test_fold_with_exactly_zero_net_pnl_is_neither_positive_nor_negative():
    ledger = [_trade("BTCUSDT", 30.0, "fold-00"), _trade("XRPUSDT", -30.0, "fold-00")]
    rows = compute_fold_stability(
        ledger=ledger, fold_selected_config={"fold-00": "S1-03"}
    )
    row = rows[0]
    assert row.positive is False  # not strictly positive
    assert row.net_pnl_class == "zero"


def test_fold_with_zero_trades_positive_is_undefined_none():
    rows = compute_fold_stability(ledger=[], fold_selected_config={"fold-00": None})
    row = rows[0]
    assert row.trade_count == 0
    assert row.positive is None
    assert row.net_expectancy_bps is None


def test_fold_with_negative_net_pnl_is_not_positive():
    ledger = [_trade("BTCUSDT", -50.0, "fold-00")]
    rows = compute_fold_stability(
        ledger=ledger, fold_selected_config={"fold-00": "S1-03"}
    )
    assert rows[0].positive is False


def test_positive_negative_zero_fold_counts_and_selected_config_frequency():
    ledger = [
        _trade("BTCUSDT", 50.0, "fold-00", config_id="S1-01"),
        _trade("BTCUSDT", -50.0, "fold-01", config_id="S1-01"),
        _trade("BTCUSDT", 10.0, "fold-02", config_id="S1-02"),
    ]
    rows = compute_fold_stability(
        ledger=ledger,
        fold_selected_config={
            "fold-00": "S1-01",
            "fold-01": "S1-01",
            "fold-02": "S1-02",
            "fold-03": None,
        },
    )
    positive = sum(1 for r in rows if r.positive is True)
    negative = sum(1 for r in rows if r.positive is False)
    zero_or_undefined = sum(1 for r in rows if r.positive is None)
    assert positive == 2
    assert negative == 1
    assert zero_or_undefined == 1
    frequency: dict[str, int] = {}
    for row in rows:
        if row.selected_config_id:
            frequency[row.selected_config_id] = (
                frequency.get(row.selected_config_id, 0) + 1
            )
    assert frequency == {"S1-01": 2, "S1-02": 1}
