# tests/test_validated_gate.py
"""ROB-320 — the gate is pure: synthetic trade lists in, verdict out.

Trades are (net_ref_pnl, commission_ref, notional, ts_opened) tuples at the
reference fee; net at any fee is recomputed analytically (mirrors fee_sweep)."""
from __future__ import annotations

from validated_gate import (
    Trade,
    evaluate_gate,
    metrics_at_fee,
    walk_forward_split,
)


def _trade(net, comm, notional, ts) -> Trade:
    return Trade(net_ref_pnl=net, commission_ref=comm, notional=notional, ts_opened=ts)


def test_walk_forward_split_is_chronological() -> None:
    trades = [_trade(1.0, -0.1, 100, ts) for ts in range(100)]
    folds = walk_forward_split(trades, fractions=(0.5, 0.25, 0.25))
    assert len(folds["train"]) == 50
    assert len(folds["val"]) == 25
    assert len(folds["oos"]) == 25
    assert max(t.ts_opened for t in folds["train"]) < min(t.ts_opened for t in folds["oos"])


def test_metrics_profit_factor_and_expectancy() -> None:
    trades = [_trade(2.0, 0.2, 100, 0), _trade(-1.0, 0.2, 100, 1)]
    m = metrics_at_fee(trades, fee_bps=0.0)  # gross (scale removes commission)
    assert m.trades == 2
    assert round(m.profit_factor, 2) == 2.75     # 2.2 / 0.8
    assert round(m.expectancy, 2) == 0.7        # (2.2 - 0.8) / 2


def test_insufficient_data_when_oos_thin() -> None:
    # 120 train, 0 oos -> insufficient
    cand = {"z2.0/tp30/sl30": [_trade(0.5, -0.1, 100, ts) for ts in range(120)]}
    report = evaluate_gate(
        candidate_runs=cand, baseline_breakout=[], baseline_random=[],
        fee_bps=10.0, min_trades=100, fractions=(1.0, 0.0, 0.0),
    )
    assert report.verdict == "insufficient_data"
    assert report.overfit_flags["low_trades"] is True


def test_not_validated_when_oos_negative() -> None:
    # plenty of trades, but OOS net is negative -> not_validated (honest)
    losing = [_trade(-0.5, -0.1, 100, ts) for ts in range(400)]
    cand = {"z2.0/tp30/sl30": losing}
    report = evaluate_gate(
        candidate_runs=cand, baseline_breakout=losing, baseline_random=losing,
        fee_bps=10.0, min_trades=100, fractions=(0.5, 0.25, 0.25),
    )
    assert report.verdict == "not_validated"


def test_validated_when_oos_positive_and_beats_baselines_and_stable() -> None:
    winners = [_trade(1.0, -0.1, 100, ts) for ts in range(400)]
    losers = [_trade(-0.5, -0.1, 100, ts) for ts in range(400)]
    cand = {
        "z2.0/tp30/sl30": winners,            # val-best and oos-best
        "z2.5/tp40/sl40": winners,            # stable across params
    }
    report = evaluate_gate(
        candidate_runs=cand, baseline_breakout=losers, baseline_random=losers,
        fee_bps=10.0, min_trades=100, fractions=(0.5, 0.25, 0.25),
    )
    assert report.verdict == "validated"
    assert report.overfit_flags["single_fold_edge"] is False
