"""ROB-382 — pure tests for the ElliotV7 (ElliotV5_SMA shape) signal port.

(a) entry fires under a hand-built synthetic case satisfying the documented condB
    (EWO deep-negative dip during a 1h uptrend), and does NOT fire when one key
    condition (the 1h uptrend gate) is broken;
(b) no-lookahead / truncation-invariance: signals(bars[:k]) agrees with
    signals(bars)[:k] outside the warmup tail;
(c) the module runs on a small real slice without error.
"""
from __future__ import annotations

import rob382_bars as rb
import rob382_signal_elliot as m

_5M_MS = 5 * 60 * 1000
_1H_MS = 60 * 60 * 1000
# Offset the 5m timeline so that by the time it starts, the 1h EMA(20/25) are already
# warmed up (the 1h series begins at ts=0). 48h of head-room is plenty for EMA(25).
_5M_OFFSET_MS = 48 * _1H_MS


def _bar(i: int, o: float, h: float, low: float, c: float, vol: float = 100.0) -> rb.OHLCVBar:
    ts = _5M_OFFSET_MS + i * _5M_MS
    return rb.OHLCVBar(ts=ts, open=o, high=h, low=low, close=c, volume=vol, close_ts=ts + _5M_MS - 1)


def _bar_1h(i: int, c: float, vol: float = 1000.0) -> rb.OHLCVBar:
    ts = i * _1H_MS
    return rb.OHLCVBar(ts=ts, open=c, high=c, low=c, close=c, volume=vol, close_ts=ts + _1H_MS - 1)


def _build_5m_series() -> list[rb.OHLCVBar]:
    """A long rising base (so EMA50 sits above EMA200 → EWO positive), then a sharp
    cliff at the end so that EMA-fast crashes far below EMA-slow (EWO << -19.988),
    rsi_fast collapses < 35, and close is well under EMA14*0.975 and EMA24*0.991."""
    bars: list[rb.OHLCVBar] = []
    # Slow, steady climb for the warmup region (>200 bars so EWO is defined).
    price = 100.0
    for i in range(260):
        price += 0.5  # gentle uptrend keeps EMA50 > EMA200 (EWO positive baseline)
        bars.append(_bar(i, price - 0.25, price + 0.5, price - 0.5, price))
    # A violent multi-bar crash: drives EMA-fast far below EMA-slow → EWO very negative,
    # and rsi_fast (period 4) collapses below 35; close falls well below the EMAs.
    p = price
    for j in range(260, 285):
        p *= 0.93  # ~7% down each 5m bar, sustained
        bars.append(_bar(j, p / 0.93, p / 0.93, p * 0.99, p))
    return bars


def _build_1h_uptrend(n_hours: int) -> list[rb.OHLCVBar]:
    """1h series in a clean uptrend so EMA(20) > EMA(25) → uptrend_1h == 1.0."""
    bars: list[rb.OHLCVBar] = []
    price = 100.0
    for i in range(n_hours):
        price += 1.0
        bars.append(_bar_1h(i, price))
    return bars


def _build_1h_downtrend(n_hours: int) -> list[rb.OHLCVBar]:
    """1h series in a clean downtrend so EMA(20) < EMA(25) → uptrend_1h == 0.0."""
    bars: list[rb.OHLCVBar] = []
    price = 1000.0
    for i in range(n_hours):
        price -= 1.0
        bars.append(_bar_1h(i, price))
    return bars


def test_entry_fires_on_documented_condB() -> None:
    bars = _build_5m_series()
    # cover the full 5m span with 1h bars (285 * 5m / 60m ≈ 24h → ~30 hourly bars; pad).
    bars_1h = _build_1h_uptrend(80)
    entry, _exit = m.signals(bars, bars_1h)
    assert len(entry) == len(bars)
    # The crash region (last ~25 bars) must produce at least one entry via condB.
    assert any(entry[260:]), "expected condB entry during the EWO-deep-negative crash dip"


def test_entry_blocked_when_uptrend_broken() -> None:
    """Breaking the single 1h-uptrend gate must suppress the entry that otherwise fires."""
    bars = _build_5m_series()
    up_1h = _build_1h_uptrend(80)
    down_1h = _build_1h_downtrend(80)

    entry_up, _ = m.signals(bars, up_1h)
    entry_down, _ = m.signals(bars, down_1h)

    assert any(entry_up[260:]), "sanity: entry should fire with the 1h uptrend present"
    # Same bars, only the 1h uptrend gate flipped off → no entries.
    assert not any(entry_down), "entry must NOT fire when the 1h uptrend gate is false"


def test_no_lookahead_truncation_invariance() -> None:
    bars = _build_5m_series()
    bars_1h = _build_1h_uptrend(80)
    full_entry, full_exit = m.signals(bars, bars_1h)

    k = 275  # inside the active region but before the very end
    # truncate the 1h pair to only bars closed as of bars[k-1] (lookahead-safe)
    cutoff_ts = bars[k - 1].ts
    bars_1h_trunc = [b for b in bars_1h if b.close_ts <= cutoff_ts]
    trunc_entry, trunc_exit = m.signals(bars[:k], bars_1h_trunc)

    assert len(trunc_entry) == k
    # Allow a small warmup tail tolerance at the very front (EMA/RSI/HMA seeding); the
    # active decision region must match bar-for-bar.
    warmup = 220
    for i in range(warmup, k):
        assert trunc_entry[i] == full_entry[i], f"entry mismatch at i={i}"
        assert trunc_exit[i] == full_exit[i], f"exit mismatch at i={i}"


def test_runs_on_real_slice() -> None:
    bars = rb.load_ohlcv("BTCUSDT", "5m")[:6000]
    bars_1h = rb.load_ohlcv("BTCUSDT", "1h")
    assert bars, "expected real BTCUSDT 5m bars on disk"
    assert bars_1h, "expected real BTCUSDT 1h bars on disk"
    entry, exit_sig = m.signals(bars, bars_1h)
    assert len(entry) == len(bars) == len(exit_sig)
    assert all(isinstance(x, bool) for x in entry)
    assert all(isinstance(x, bool) for x in exit_sig)
