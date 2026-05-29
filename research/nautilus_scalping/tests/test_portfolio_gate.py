"""ROB-351 (eng-review Issue 1) — portfolio-return path for basket / XS strategies.

validated_gate's per-trade equity sum (metrics_at_fee) is correct for a
single-symbol serial scalper but UNDERSTATES drawdown when a basket holds
concurrent positions that move together. These tests pin the correctness gap and
the new period-return path that fixes it.
"""

import validated_gate
from validated_gate import PortfolioPeriod, Trade


def test_period_net_uses_shared_primitive():
    # one period: gross 5 at ref, commission 2 -> zero-fee adds it back
    p = PortfolioPeriod(ts=1, gross_ref_pnl=5.0, commission_ref=2.0)
    assert validated_gate.portfolio_net_pnls_at_fee([p], 10.0) == [5.0]
    assert validated_gate.portfolio_net_pnls_at_fee([p], 0.0) == [7.0]


def test_portfolio_drawdown_beats_serial_trade_drawdown():
    """The Issue-1 bug, made concrete.

    Three positions all open at t=0, all drop -10 simultaneously at t=1, then each
    recovers and closes net +1 at t=2,3,4.

      * Flat Trade list (per position, keyed by ts_opened): metrics_at_fee sees
        three +1 trades in sequence -> equity 1,2,3 -> drawdown 0 (WRONG: hides
        that the book was -30 underwater at once).
      * Portfolio period series: t1 = -30 (all three down together) then recovery
        -> equity dips to -30 -> drawdown -30 (CORRECT).
    """
    flat_trades = [
        Trade(net_ref_pnl=1.0, commission_ref=0.0, notional=100.0, ts_opened=0),
        Trade(net_ref_pnl=1.0, commission_ref=0.0, notional=100.0, ts_opened=0),
        Trade(net_ref_pnl=1.0, commission_ref=0.0, notional=100.0, ts_opened=0),
    ]
    serial = validated_gate.metrics_at_fee(flat_trades, 0.0)
    assert serial.max_drawdown == 0.0  # the bug: zero drawdown

    periods = [
        PortfolioPeriod(ts=1, gross_ref_pnl=-30.0, commission_ref=0.0),
        PortfolioPeriod(ts=2, gross_ref_pnl=11.0, commission_ref=0.0),
        PortfolioPeriod(ts=3, gross_ref_pnl=11.0, commission_ref=0.0),
        PortfolioPeriod(ts=4, gross_ref_pnl=11.0, commission_ref=0.0),
    ]
    pf = validated_gate.portfolio_metrics_at_fee(periods, 0.0)
    assert pf.max_drawdown == -30.0          # correct, severe
    assert pf.net_pnl == 3.0                 # same total as the flat trades
    assert pf.max_drawdown < serial.max_drawdown  # portfolio path is the honest one


def test_evaluate_gate_portfolio_returns_report_with_portfolio_dd():
    # enough periods to split walk-forward and exceed a small min_trades
    periods = [
        PortfolioPeriod(ts=i, gross_ref_pnl=(1.0 if i % 2 else -0.5), commission_ref=0.1)
        for i in range(1, 41)
    ]
    rep = validated_gate.evaluate_gate_portfolio(
        candidate_runs={"p": periods},
        baseline_periods=[PortfolioPeriod(ts=i, gross_ref_pnl=0.0, commission_ref=0.1)
                          for i in range(1, 41)],
        fee_bps=2.0,
        min_periods=5,
        candidate_name="basket-test",
    )
    assert "gross" in rep.results
    assert "net_after_cost" in rep.results
    # portfolio DD present and computed on the period equity curve (<= 0)
    assert rep.results["net_after_cost"]["max_drawdown"] <= 0.0
    assert rep.verdict in ("validated", "not_validated", "insufficient_data")
