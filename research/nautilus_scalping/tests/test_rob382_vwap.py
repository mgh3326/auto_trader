"""ROB-382 — pure tests for the ported VWAPStrategy (@jilv220) signal.

Covers:
  (a) entry fires under a hand-built synthetic case satisfying the documented condition,
      and does NOT fire when one key condition (RSI-14) is broken;
  (b) no-lookahead / truncation-invariance: signals(bars[:k]) agrees with signals(bars)[:k]
      on the non-warmup region (small warmup-tail tolerance);
  (c) the module runs on a small real slice without error.

Pure (stdlib + harness modules only); no network, no freqtrade/talib/pandas.
"""
from __future__ import annotations

import rob382_bars as rb
import rob382_signal_vwap as m
from rob382_bars import OHLCVBar


def _bar(ts: int, o: float, h: float, lo: float, c: float, v: float = 100.0) -> OHLCVBar:
    return OHLCVBar(ts=ts, open=o, high=h, low=lo, close=c, volume=v, close_ts=ts + 299_999)


def _build_decline(n_pre: int = 130) -> list[OHLCVBar]:
    """A long, steady, accelerating-at-the-end decline.

    The sustained downtrend drives RSI-14/84/112 low and CTI strongly negative; the last
    bar adds a sharp gap-down so the current close sits below the VWAP lower band AND
    >4% under the rolling-max open over the last 4 bars (tcp_percent_4 > 0.04).
    """
    bars: list[OHLCVBar] = []
    price = 1000.0
    ts0 = 1_700_000_000_000
    step = 300_000  # 5m in ms
    # Steady decline: each bar opens at prev close, falls ~0.6%/bar.
    for i in range(n_pre):
        o = price
        c = price * 0.994
        h = o * 1.0005
        lo = c * 0.9995
        bars.append(_bar(ts0 + i * step, o, h, lo, c))
        price = c
    # Final sharp drop: a big down bar (open near prev close, close ~5% lower).
    o = price
    c = price * 0.95
    h = o * 1.0002
    lo = c * 0.999
    bars.append(_bar(ts0 + n_pre * step, o, h, lo, c))
    return bars


def test_entry_fires_on_documented_condition():
    bars = _build_decline()
    entry, exit_sig = m.signals(bars)
    assert len(entry) == len(bars)
    assert len(exit_sig) == len(bars)
    # Exit is empty in the source (roi_sl handled by EXIT_MODEL).
    assert not any(exit_sig)
    # Entry should fire on the final sharp-drop bar.
    assert entry[-1] is True, "entry must fire when all VWAP-dip conditions hold"


def test_entry_does_not_fire_when_rsi_condition_broken():
    """Break ONLY the RSI-14 < 35 gate by ending on a strong up bar.

    A rally on the final bar lifts RSI-14 above 35 (and also pushes close above the VWAP
    band / removes the tcp gap), so entry must NOT fire.
    """
    bars = _build_decline()
    last = bars[-1]
    # Replace the final bar with a strong RECOVERY bar: close well above its open.
    up = OHLCVBar(
        ts=last.ts,
        open=last.open,
        high=last.open * 1.06,
        low=last.open * 0.999,
        close=last.open * 1.05,  # rally → RSI-14 lifts, close above vwap_low, tcp gap gone
        volume=last.volume,
        close_ts=last.close_ts,
    )
    bars = bars[:-1] + [up]
    entry, _ = m.signals(bars)
    assert entry[-1] is False, "entry must NOT fire when the RSI/dip condition is broken"


def test_no_lookahead_truncation_invariance():
    """signals(bars[:k]) must equal signals(bars)[:k] outside the warmup tail.

    Causal logic means truncating the future cannot change a past signal. The slowest
    indicator is RSI-112, so anything at/after index 112 is in the settled region; we
    compare on indices < k that are also past a generous warmup boundary.
    """
    bars = rb.load_ohlcv("BTCUSDT", "5m")[:6000]
    assert bars, "real 5m data must be available"
    full_entry, full_exit = m.signals(bars)

    warmup = 200  # comfortably past RSI-112 / vwap-band warmup
    for k in (3000, 4500):
        ke, kx = m.signals(bars[:k])
        assert len(ke) == k
        # Compare settled region [warmup, k). The truncated run loses no PAST info, so
        # signals must match exactly here (no warmup-tail near k for ENTRY because the
        # window is fully contained; we still allow the very last few bars as tolerance).
        tail_tol = 0  # entry at i depends only on bars[0..i]; full containment → exact
        hi = k - tail_tol
        for i in range(warmup, hi):
            assert ke[i] == full_entry[i], f"entry lookahead at i={i}, k={k}"
            assert kx[i] == full_exit[i], f"exit lookahead at i={i}, k={k}"


def test_runs_on_real_slice_without_error():
    bars = rb.load_ohlcv("BTCUSDT", "5m")[:6000]
    assert bars, "real 5m data must be available"
    entry, exit_sig = m.signals(bars)
    assert len(entry) == len(bars) == len(exit_sig)
    assert all(isinstance(x, bool) for x in entry)
    assert not any(exit_sig)  # source has no exit signal
    # Sanity: this is a selective dip-buy; entries should be a small minority (or zero on
    # a slice), never every bar.
    assert sum(entry) <= len(bars) // 2
