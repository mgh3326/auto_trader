"""ROB-351 (T7) — family 1-3 signal-generator MECHANICS.

These pin the signal/position/PnL/cost mechanics on synthetic fixtures. They do
NOT assert any empirical edge — the real campaign RUN on Binance USD-M data is the
operator's PR2 step (no market data is committed). Family 1 emits single-symbol
``Trade`` lists; families 2-3 emit ``PortfolioPeriod`` series for the portfolio gate.
"""

import cost_model
import families
from validated_gate import PortfolioPeriod, Trade, portfolio_metrics_at_fee


def test_make_taker_trade_gross_and_commission():
    t = families.make_taker_trade(gross_pnl=5.0, ts=1, notional=1000.0, ref_fee_bps=10.0)
    assert isinstance(t, Trade)
    # zero-fee net == gross; commission is the 2-leg ref fee on notional
    assert abs(cost_model.net_at_fee(t.net_ref_pnl, t.commission_ref, 0.0) - 5.0) < 1e-9
    assert abs(t.commission_ref - 2 * 10.0 / 1e4 * 1000.0) < 1e-9


def test_breakout_continuation_enters_on_breakout():
    # flat then a clean breakout + continuation
    bars = [families.Bar(ts=i, high=100.0, low=99.0, close=100.0) for i in range(20)]
    bars += [families.Bar(ts=20, high=105.0, low=100.0, close=105.0),
             families.Bar(ts=21, high=107.0, low=104.0, close=107.0),
             families.Bar(ts=22, high=109.0, low=106.0, close=109.0)]
    trades = families.breakout_continuation_trades(bars, lookback=20, hold=2)
    assert len(trades) >= 1
    assert all(isinstance(t, Trade) for t in trades)


def test_breakout_continuation_flat_market_no_trades():
    bars = [families.Bar(ts=i, high=100.0, low=99.0, close=99.5) for i in range(40)]
    assert families.breakout_continuation_trades(bars, lookback=20, hold=2) == []


def test_ts_trend_basket_longs_up_shorts_down():
    # AAA trends up, BBB trends down; trend basket should be net-positive gross
    up = [(i, 100.0 + i) for i in range(40)]
    down = [(i, 100.0 - i) for i in range(40)]
    periods = families.ts_trend_basket_periods({"AAA": up, "BBB": down}, lookback=10)
    assert all(isinstance(p, PortfolioPeriod) for p in periods)
    assert portfolio_metrics_at_fee(periods, 0.0).net_pnl > 0  # gross edge on the fixture


def test_xs_momentum_periods_emit_and_respect_lookback():
    closes = {
        "AAA": [(i, 100.0 + 2 * i) for i in range(40)],   # strongest momentum
        "BBB": [(i, 100.0 + i) for i in range(40)],
        "CCC": [(i, 100.0 - i) for i in range(40)],       # weakest
    }
    rebalances = [20, 25, 30]
    periods = families.xs_momentum_periods(closes, rebalances=rebalances, lookback=10, top_k=1)
    assert all(isinstance(p, PortfolioPeriod) for p in periods)
    assert len(periods) >= 1
