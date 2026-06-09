"""ROB-382 — pure tests for the ichiV1 signal port (no freqtrade/talib/pandas).

Covers:
  (a) entry fires on a hand-built case that satisfies the documented condition, and does
      NOT fire when one key condition (fan rising / above-cloud) is broken;
  (b) no-lookahead / truncation-invariance: signals(bars[:k]) == signals(bars)[:k] over
      the non-warmup region (small warmup-tail tolerance);
  (c) the module runs on a small real slice without error.
"""
from __future__ import annotations

import rob382_bars as rb
import rob382_signal_ichi as m


def _bar(ts, o, h, l, c, v=100.0):
    return rb.OHLCVBar(ts=ts, open=o, high=h, low=l, close=c, volume=v, close_ts=ts + 299_999)


def _uptrend_bars(n, start=100.0, base=0.003, accel=0.0002):
    """ACCELERATING monotone uptrend so EMAs stack bullishly, the real close sits well above
    the lagging cloud, and fan_magnitude = EMA12/EMA96 keeps WIDENING bar-to-bar (a constant
    growth rate stabilises the fan ratio → gain ~1.0 < 1.002; the strategy needs the fan to
    be actively expanding, so the per-bar step grows linearly)."""
    bars = []
    price = start
    for i in range(n):
        step = base + i * accel
        o = price
        c = price * (1.0 + step)
        h = c * 1.0005
        lo = o * 0.9998
        bars.append(_bar(1_700_000_000_000 + i * 300_000, o, h, lo, c))
        price = c
    return bars


def test_entry_fires_on_documented_uptrend():
    # Long enough to clear the 96-period EMA8h + ichimoku(120)+displacement(30) warmup.
    bars = _uptrend_bars(400)
    entry, exit_sig = m.signals(bars)
    assert len(entry) == len(bars) == len(exit_sig)
    # On a clean sustained uptrend, the entry must fire somewhere in the mature region.
    assert any(entry[200:]), "expected entry to fire in the mature region of a clean uptrend"
    # The exit signal (close x-under EMA24) must NOT fire on a strictly rising series.
    assert not any(exit_sig), "exit (cross-below) should never fire on a monotone uptrend"


def test_entry_blocked_when_below_cloud():
    """Break the 'above senkou' condition by flipping the late series into a steep
    downtrend (price falls below the lagging cloud) — entry must not fire there."""
    up = _uptrend_bars(250)
    # Continue with a sustained downtrend so the recent closes fall below the cloud.
    bars = list(up)
    price = up[-1].close
    for i in range(150):
        o = price
        c = price * (1.0 - 0.004)
        h = o * 1.0002
        lo = c * 0.9995
        bars.append(_bar(1_700_000_000_000 + (250 + i) * 300_000, o, h, lo, c))
        price = c
    entry, _ = m.signals(bars)
    # In the downtrend tail, every above-cloud + fan-rising condition is broken.
    assert not any(entry[300:]), "entry must not fire while price is in a downtrend below the cloud"


def test_entry_blocked_when_fan_not_rising():
    """Break the fan-rising condition: a FLAT series has fan_magnitude == 1 and a gain of
    exactly 1.0 (< 1.002), and is not above its own cloud strictly — so no entry."""
    flat = [
        _bar(1_700_000_000_000 + i * 300_000, 100.0, 100.05, 99.95, 100.0)
        for i in range(400)
    ]
    entry, _ = m.signals(flat)
    assert not any(entry), "flat series must not fire entry (fan gain == 1.0, fan == 1)"


def test_no_lookahead_truncation_invariance():
    """signals(bars[:k]) must agree with signals(bars)[:k] on the non-warmup region.

    Causality means a prefix recompute cannot differ from the full run, except possibly in
    the warmup tail where one extra forming bar can flip a boundary EMA seed.
    """
    bars = _uptrend_bars(360)
    full_entry, full_exit = m.signals(bars)
    k = 300
    pre_entry, pre_exit = m.signals(bars[:k])
    assert len(pre_entry) == k
    # Compare the stable region (well past the 96+30+120 warmup), leaving a small tail
    # tolerance just below the truncation point for forming-bar boundary effects.
    warmup = 256
    tail_tol = 3
    hi = k - tail_tol
    assert full_entry[warmup:hi] == pre_entry[warmup:hi], "entry differs under truncation (lookahead?)"
    assert full_exit[warmup:hi] == pre_exit[warmup:hi], "exit differs under truncation (lookahead?)"


def test_runs_on_real_slice():
    bars = rb.load_ohlcv("BTCUSDT", "5m")
    assert bars, "expected real BTCUSDT 5m bars on disk"
    sl = bars[:6000]
    entry, exit_sig = m.signals(sl)
    assert len(entry) == len(sl) == len(exit_sig)
    assert all(isinstance(x, bool) for x in entry[:50])
    assert all(isinstance(x, bool) for x in exit_sig[:50])
    # Sanity: the module produced at least one entry across 6000 real bars (not a hard
    # requirement of correctness, but confirms the gate isn't degenerate on real data).
    assert any(entry), "expected at least one entry across 6000 real BTCUSDT 5m bars"
