import families

from external_strategy_sieve.validation.signals import (
    bbrsi_trades,
    chandelier_trades,
    supertrend_trades,
)


def _bars_from_closes(closes, spread=1.0):
    return [
        families.Bar(ts=i * 60000, high=c + spread, low=c - spread, close=c)
        for i, c in enumerate(closes)
    ]


def _up_then_down(n=60, peak=160.0, base=100.0):
    up = [base + (peak - base) * i / (n - 1) for i in range(n)]
    down = [peak - (peak - base) * i / (n - 1) for i in range(n)]
    return up + down


def test_supertrend_flat_series_no_trades():
    bars = _bars_from_closes([100.0] * 50)
    assert supertrend_trades(bars, atr_period=10, multiplier=3.0) == []


def test_supertrend_up_then_down_yields_a_completed_long():
    bars = _bars_from_closes(_up_then_down())
    trades = supertrend_trades(bars, atr_period=10, multiplier=3.0)
    assert len(trades) >= 1
    assert trades[0].net_ref_pnl + abs(trades[0].commission_ref) > 0


def test_chandelier_up_then_down_yields_trades():
    bars = _bars_from_closes(_up_then_down())
    trades = chandelier_trades(bars, atr_period=10, multiplier=3.0)
    assert len(trades) >= 1


def test_range_filter_up_then_down_yields_trades():
    from external_strategy_sieve.validation.signals import range_filter_trades

    bars = _bars_from_closes(_up_then_down())
    trades = range_filter_trades(bars, period=10, mult=1.0)
    assert len(trades) >= 1


def test_signals_are_deterministic():
    bars = _bars_from_closes(_up_then_down())
    assert supertrend_trades(bars) == supertrend_trades(bars)


def test_bbrsi_v_shape_yields_long_round_trip():
    closes = (
        [100.0] * 25
        + [100 - 3 * i for i in range(1, 16)]
        + [55 + 3 * i for i in range(1, 30)]
    )
    bars = _bars_from_closes(closes)
    trades = bbrsi_trades(bars, bb_period=20, bb_k=2.0, rsi_period=14, rsi_oversold=35)
    assert len(trades) >= 1
    assert all(t.notional == 1000.0 for t in trades)


def test_squeeze_momentum_runs_and_is_deterministic():
    from external_strategy_sieve.validation.signals import squeeze_momentum_trades

    closes = [100.0 + 0.05 * (i % 2) for i in range(40)] + _up_then_down(
        30, 130.0, 100.0
    )
    bars = _bars_from_closes(closes)
    trades = squeeze_momentum_trades(bars, length=20, bb_k=2.0, kc_mult=1.5)
    assert squeeze_momentum_trades(bars) == squeeze_momentum_trades(bars)
    assert isinstance(trades, list)


def test_squeeze_flat_series_no_trades():
    from external_strategy_sieve.validation.signals import squeeze_momentum_trades

    bars = _bars_from_closes([100.0] * 60)
    assert squeeze_momentum_trades(bars) == []
