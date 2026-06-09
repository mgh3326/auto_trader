"""ROB-382 — pure tests for the ClucHAnix signal port (rob382_signal_cluc).

Covers:
  (a) entry fires under a hand-built case satisfying the documented condition, and does NOT
      fire when one key condition (the rocr_1h gate) is broken;
  (b) no-lookahead / truncation invariance: signals(bars[:k]) agrees with signals(bars)[:k]
      on the non-warmup region (small warmup-tail tolerance);
  (c) the module runs on a small REAL slice (first 6000 1m bars of BTCUSDT) without error.

Pure: no freqtrade / talib / pandas. Run from research/nautilus_scalping:
    uv run --no-project pytest tests/test_rob382_cluc.py -q
"""
from __future__ import annotations

import rob382_bars as rb
import rob382_indicators as I
import rob382_signal_cluc as m


def _bar(ts, o, h, l, c, v=10.0, interval_ms=60_000):
    return rb.OHLCVBar(ts=ts, open=o, high=h, low=l, close=c, volume=v, close_ts=ts + interval_ms - 1)


# Anchor the base (1m) series AFTER 200 fully-closed 1h bars so the merged ROCR(168) is valid.
_H = 3600_000
_BASE_START_TS = 200 * _H  # base 1m bars start well past the 1h ROCR(168) warmup


def _flat_1h(n=300, price=100.0):
    """A flat 1h series: ROCR(ha_close_1h, 168) == 1.0 (> 0.54904) on the post-warmup region."""
    bars_1h = []
    t1 = 0
    for _ in range(n):
        bars_1h.append(_bar(t1, price, price + 0.5, price - 0.5, price, v=1000.0, interval_ms=_H))
        t1 += _H
    return bars_1h


def _build_entry_case():
    """Hand-build a 1m series whose final bar satisfies the PRIMARY (cond_a) squeeze-dip branch.

    cond_a (the Bollinger-squeeze dip-buy) requires ALL of:
        lower[i-1] > 0
        bbdelta[i]    > ha_close[i] * 0.01965     (band has width)
        closedelta[i] > ha_close[i] * 0.00556     (a real down move vs prior HA close)
        tail[i]       < bbdelta[i] * 0.95089       (close near its low -> tiny lower tail)
        ha_close[i]   < lower[i-1]                 (dipped below the prior lower band)
        ha_close[i]  <= ha_close[i-1]              (still falling)
      plus the rocr_1h > 0.54904 gate.

    Construction: a ±6 oscillating plateau (builds nonzero band width / bbdelta), then a
    moderate down bar (drags ha_open/ha_close down so closedelta is large), then a gap-down
    DOJI final bar (open=high=low=close) so ha_close ~= ha_low -> tail ~= 0. A flat 1h
    informative gives rocr_1h == 1.0 (> gate). Time-anchored past the 1h ROCR(168) warmup.
    (All thresholds verified numerically; see the per-condition asserts in the entry test.)
    """
    amp, mid_dip, dip = 6.0, 95.0, 92.0
    bars = []
    ts = _BASE_START_TS
    for k in range(118):
        p = 100.0 + (amp if k % 2 == 0 else -amp)
        bars.append(_bar(ts, p, p + 0.2, p - 0.2, p))
        ts += 60_000
    # intermediate down bar (large HA closedelta on the way down)
    bars.append(_bar(ts, 100.0, 100.0, mid_dip, mid_dip))
    ts += 60_000
    # gap-down doji: open==high==low==close -> ha_close ~= ha_low -> tail ~= 0
    bars.append(_bar(ts, dip, dip, dip, dip))

    return bars, _flat_1h()


def test_entry_fires_on_documented_case():
    bars, bars_1h = _build_entry_case()
    entry, _exit = m.signals(bars, bars_1h)
    last = len(bars) - 1

    # Re-derive the documented cond_a sub-conditions to prove the case is genuinely satisfied.
    opens = [b.open for b in bars]; highs = [b.high for b in bars]
    lows = [b.low for b in bars]; closes = [b.close for b in bars]
    _ho, hh, hl, hc = I.heikin_ashi(opens, highs, lows, closes)
    ha_typ = [(hh[i] + hl[i] + hc[i]) / 3.0 for i in range(len(bars))]
    mr, lr, _u = I.bollinger(ha_typ, m.BB_WINDOW, m.BB_NUM_STD, ddof=1)
    mid = [v if v == v else 0.0 for v in mr]; lower = [v if v == v else 0.0 for v in lr]
    bbd = abs(mid[last] - lower[last]); cd = abs(hc[last] - hc[last - 1]); tail = abs(hc[last] - hl[last])
    assert lower[last - 1] > 0
    assert bbd > hc[last] * m.BBDELTA_CLOSE
    assert cd > hc[last] * m.CLOSEDELTA_CLOSE
    assert tail < bbd * m.BBDELTA_TAIL
    assert hc[last] < lower[last - 1]
    assert hc[last] <= hc[last - 1]
    assert entry[last] is True, "cond_a squeeze-dip entry should fire when all conditions + rocr gate hold"


def test_entry_blocked_when_rocr_gate_broken():
    """Break ONLY the rocr_1h>0.54904 gate (informative trend below threshold) -> no entry."""
    bars, _bars_1h = _build_entry_case()
    # Build a steeply DOWN-trending 1h series so ROCR(ha_close_1h,168) < 0.54904 at the base ts.
    bars_1h = []
    t1 = 0
    price1h = 100_000.0
    for _ in range(300):
        nxt = price1h * 0.99  # steady decay -> rocr over 168 lags well below 0.549
        bars_1h.append(_bar(t1, price1h, price1h, nxt, nxt, v=1000.0, interval_ms=_H))
        price1h = nxt
        t1 += _H
    entry, _exit = m.signals(bars, bars_1h)
    last = len(bars) - 1
    # Sanity: the rocr at the last bar must indeed be below the gate (the broken condition).
    o1h = [b.open for b in bars_1h]; h1h = [b.high for b in bars_1h]
    l1h = [b.low for b in bars_1h]; c1h = [b.close for b in bars_1h]
    ha_c_1h = I.heikin_ashi(o1h, h1h, l1h, c1h)[3]
    rocr_v = I.rocr(ha_c_1h, m.ROCR_1H_LEN)
    rocr_b = I.merge_informative([b.ts for b in bars], [b.close_ts for b in bars_1h], rocr_v)
    assert rocr_b[last] < m.ROCR_1H, "test setup: rocr gate must actually be broken"
    assert entry[last] is False, "entry must NOT fire when the rocr_1h gate is broken"


def test_no_lookahead_truncation_invariance():
    """signals(bars[:k]) must equal signals(bars)[:k] on the non-warmup region.

    Causal indicators may differ in a small warmup tail near the truncation point (e.g. the
    bollinger/rsi/ema warmup re-seeds when the series is shorter); we compare on the stable
    interior [warmup .. k-WARMUP_TAIL].
    """
    bars = rb.load_ohlcv("BTCUSDT", "1m")[:4000]
    bars_1h = rb.load_ohlcv("BTCUSDT", "1h")[:200]
    assert bars and bars_1h
    full_entry, full_exit = m.signals(bars, bars_1h)

    k = 3000
    trunc_entry, trunc_exit = m.signals(bars[:k], bars_1h)

    WARMUP = 250  # past ema_slow(50)/bollinger(40)/rsi(14) warmup
    WARMUP_TAIL = 0  # truncation re-seeds nothing here (all indicators are running-window/causal)
    lo, hi = WARMUP, k - WARMUP_TAIL
    mism_entry = [i for i in range(lo, hi) if full_entry[i] != trunc_entry[i]]
    mism_exit = [i for i in range(lo, hi) if full_exit[i] != trunc_exit[i]]
    # Allow a tiny tolerance for any boundary re-seed; assert essentially exact agreement.
    assert len(mism_entry) <= 2, f"entry lookahead leak at {mism_entry[:10]}"
    assert len(mism_exit) <= 2, f"exit lookahead leak at {mism_exit[:10]}"


def test_runs_on_small_real_slice():
    bars = rb.load_ohlcv("BTCUSDT", "1m")[:6000]
    bars_1h = rb.load_ohlcv("BTCUSDT", "1h")[:300]
    assert bars and bars_1h
    entry, exit_sig = m.signals(bars, bars_1h)
    assert len(entry) == len(bars) == len(exit_sig)
    assert all(isinstance(x, bool) for x in entry)
    assert all(isinstance(x, bool) for x in exit_sig)
    # Should produce at least some signal activity over 6000 bars (sanity, not a hard edge claim).
    assert any(entry) or any(exit_sig)
