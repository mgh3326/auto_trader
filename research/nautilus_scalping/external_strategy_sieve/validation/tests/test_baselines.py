import families

from external_strategy_sieve.validation.baselines import (
    breakout_baseline,
    random_entry_trades,
)


def _bars(n):
    return [
        families.Bar(ts=i, high=100 + i + 1, low=100 + i - 1, close=100.0 + i)
        for i in range(n)
    ]


def test_random_entry_is_turnover_matched_and_seeded():
    bars = _bars(200)
    a = random_entry_trades(bars, n_trades=50, hold=5, seed=42)
    b = random_entry_trades(bars, n_trades=50, hold=5, seed=42)
    assert len(a) == 50
    assert [t.net_ref_pnl for t in a] == [t.net_ref_pnl for t in b]


def test_breakout_baseline_returns_trades():
    bars = _bars(200)
    assert isinstance(breakout_baseline(bars), list)
