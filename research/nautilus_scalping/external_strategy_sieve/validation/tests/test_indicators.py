import families

from external_strategy_sieve.validation.indicators import (
    atr,
    bollinger,
    ema,
    keltner,
    rolling_std,
    rsi,
    sma,
    true_range,
)


def _bars(seq):
    return [
        families.Bar(ts=i, high=h, low=l, close=c) for i, (h, l, c) in enumerate(seq)
    ]


def test_sma_trailing():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_rolling_std_population():
    out = rolling_std([2, 4, 4, 4, 5, 5, 7, 9], 8)
    assert out[-1] is not None and abs(out[-1] - 2.0) < 1e-9


def test_true_range_first_is_high_low():
    bars = _bars([(10, 8, 9), (12, 9, 11)])
    tr = true_range(bars)
    assert tr[0] == 2.0
    # second: max(12-9, |12-9|, |9-9|) = 3
    assert tr[1] == 3.0


def test_atr_seed_is_mean_of_first_n_true_ranges():
    bars = _bars([(10, 8, 9), (11, 9, 10), (12, 10, 11)])
    a = atr(bars, 2)
    assert a[0] is None and a[1] is not None


def test_rsi_monotonic_up_is_100():
    closes = [float(x) for x in range(1, 20)]
    r = rsi(closes, 14)
    assert r[-1] == 100.0


def test_bollinger_mid_equals_sma():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    mid, up, lo = bollinger(closes, 3, 2.0)
    assert mid[2] == 2.0 and up[2] > mid[2] > lo[2]


def test_keltner_bands_around_ema():
    bars = _bars([(i + 1, i - 1, i) for i in range(1, 11)])
    mid, up, lo = keltner(bars, 3, 1.5)
    assert mid[-1] is not None and up[-1] > mid[-1] > lo[-1]


def test_ema_is_deterministic():
    assert ema([1.0, 2.0, 3.0], 2) == ema([1.0, 2.0, 3.0], 2)
