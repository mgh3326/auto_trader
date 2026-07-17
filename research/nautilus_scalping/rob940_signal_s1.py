"""ROB-943 (H3, ROB-940) — S1 Donchian-15m signal generator (pure, stdlib).

Implements the ROB-940 research draft's Strategy 1 exactly (current-bar-
excluded Donchian breakout, volume confirmation, ATR volatility/chase
gates), scoped per the frozen 12-config manifest (``rob940_signal_manifest``)
and emitting ``rob940_engine.SignalEvent`` instances ready for
``run_symbol_stream`` — no execution/cost/arbitration logic lives here.

ultrathink (ATR seed): Wilder's ATR is a CONTEMPORANEOUS indicator — the
signal at bar t's close is allowed to use bar t's own True Range (the bar has
already fully closed by signal time). "current-bar leakage" in the ROB-943
prompt refers to Donchian U/D (which DO exclude the current bar) and to
never using bar t+1 data, not to lagging ATR by one bar. Seed = simple
average of TR_1..TR_N (both endpoints inclusive, 1-indexed within a
contiguous segment so TR_1 uses bar[0] as the previous close); this matches
the standard Wilder definition and is available starting at bar index
``period`` (0-indexed) within a segment. Recurrence:
``ATR_t = (ATR_{t-1}*(period-1) + TR_t) / period`` for t > period.

ultrathink (segment/gap reset): ``AggregatedBar.is_segment_start`` (set by
``rob940_bars_agg.aggregate_complete``) is the ONLY gap signal this module
needs — consecutive bars within a segment are guaranteed contiguous by that
module's contract, so indicator state (ATR/Donchian/volume-median buffers)
resets to a fresh, zero-indexed accumulation exactly when that flag is True.
No prior-segment bar (including its close) is ever referenced across a gap.

ultrathink (I4, ROB-943 R1 remediation): ``get_s1_config`` only fails closed
for callers that go THROUGH it. ``generate_s1_signals`` now asserts exact
frozen-manifest membership (symbol + config, by VALUE not identity) as its
very first act, before touching ``bars_15m`` at all — a caller handing in a
forged/tampered/unregistered ``S1Config`` or an out-of-universe symbol must
never reach the math loop, even with zero bars.

No DB/network/app/broker/order/fill/scheduler/random/current-time imports —
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from rob940_bars_agg import AggregatedBar
from rob940_engine import SignalEvent
from rob940_signal_manifest import (
    FrozenSignalConstants,
    S1Config,
    _validate_symbol,
    assert_matches_frozen_s1_config,
)

_C = FrozenSignalConstants


def _segment_slices(bars: Sequence[AggregatedBar]) -> list[tuple[int, int]]:
    """Return (start, stop) index pairs for each contiguous segment."""
    if not bars:
        return []
    bounds: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(bars)):
        if bars[i].is_segment_start:
            bounds.append((start, i))
            start = i
    bounds.append((start, len(bars)))
    return bounds


def _wilder_atr_series(
    bars: Sequence[AggregatedBar], period: int
) -> list[float | None]:
    """Wilder ATR for ONE contiguous segment; index i is ``None`` until warm.

    ``bars`` must already be a single gap-free segment (callers slice per
    segment via ``_segment_slices``). TR at index 0 is undefined (no prior
    close within the segment) and is never computed or used.
    """
    n = len(bars)
    atr: list[float | None] = [None] * n
    if n <= period:
        return atr
    trs: list[float | None] = [None] * n
    for i in range(1, n):
        prev_close = bars[i - 1].close
        h, low = bars[i].high, bars[i].low
        trs[i] = max(h - low, abs(h - prev_close), abs(low - prev_close))
    seed_values = trs[1 : period + 1]
    running = sum(seed_values) / period  # type: ignore[arg-type]
    atr[period] = running
    for i in range(period + 1, n):
        running = (running * (period - 1) + trs[i]) / period  # type: ignore[operator]
        atr[i] = running
    return atr


def _rolling_median(values: Sequence[float], window: int, idx: int) -> float | None:
    """Median of ``values[idx-window:idx]`` (current index excluded), or
    ``None`` if fewer than ``window`` prior values exist.
    """
    if idx < window:
        return None
    return statistics.median(values[idx - window : idx])


def _rolling_max(values: Sequence[float], window: int, idx: int) -> float | None:
    if idx < window:
        return None
    return max(values[idx - window : idx])


def _rolling_min(values: Sequence[float], window: int, idx: int) -> float | None:
    if idx < window:
        return None
    return min(values[idx - window : idx])


def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _assert_unique_signal_ts(signals: Sequence[SignalEvent]) -> None:
    """Fail-closed guard: a (strategy,config,symbol) stream must never carry
    two signals with the same ``signal_ts`` (H2 caller precondition, AC/§6).
    """
    seen: set[int] = set()
    for sig in signals:
        if sig.signal_ts in seen:
            raise ValueError(
                f"duplicate signal_ts {sig.signal_ts} for {sig.symbol}/"
                f"{sig.config_id} — signal generation must be one-per-bar"
            )
        seen.add(sig.signal_ts)


def generate_s1_signals(
    bars_15m: Sequence[AggregatedBar],
    config: S1Config,
    *,
    symbol: str,
    fold_id: str | None = None,
) -> tuple[SignalEvent, ...]:
    """Generate the S1 (Donchian-15m) signal stream for ONE symbol/config.

    ``bars_15m`` is the complete-only 15m aggregation of one symbol's 1m
    bars (``rob940_bars_agg.aggregate_complete(..., bucket_minutes=15)``).
    Fixed constants (ATR period, a_t band, chase cap, timeout/cooldown) come
    from ``FrozenSignalConstants``; the four free parameters come from the
    frozen ``S1Config`` row. Returns signals in input (chronological) order,
    already unique by ``signal_ts``.
    """
    _validate_symbol(symbol)
    assert_matches_frozen_s1_config(config)

    out: list[SignalEvent] = []
    min_idx = max(config.L, _C.ATR_PERIOD)

    for seg_start, seg_stop in _segment_slices(bars_15m):
        seg = bars_15m[seg_start:seg_stop]
        atr_series = _wilder_atr_series(seg, _C.ATR_PERIOD)
        highs = [b.high for b in seg]
        lows = [b.low for b in seg]
        volumes = [b.volume for b in seg]

        for i in range(min_idx, len(seg)):
            atr = atr_series[i]
            if atr is None or not math.isfinite(atr) or atr <= 0:
                continue
            bar = seg[i]
            c = bar.close
            if not math.isfinite(c) or c <= 0:
                continue

            vol_median = _rolling_median(volumes, _C.VOLUME_MEDIAN_WINDOW, i)
            if vol_median is None or not math.isfinite(vol_median) or vol_median <= 0:
                continue
            q = bar.volume / vol_median

            a_t = atr / c
            if not (_C.A_T_MIN <= a_t <= _C.A_T_MAX):
                continue
            if q < config.q_min:
                continue

            u = _rolling_max(highs, config.L, i)
            d = _rolling_min(lows, config.L, i)
            if u is None or d is None:
                continue

            side: str | None = None
            if c > u:
                chase = (c - u) / atr
                if 0 < chase <= _C.CHASE_MAX_ATR_MULT:
                    side = "long"
            elif c < d:
                chase = (d - c) / atr
                if 0 < chase <= _C.CHASE_MAX_ATR_MULT:
                    side = "short"

            if side is None:
                continue

            d_sl = _clip(
                config.k_SL * a_t,
                _C.S1_SL_CLIP_MIN_BPS / 1e4,
                _C.S1_SL_CLIP_MAX_BPS / 1e4,
            )
            d_tp = config.R_TP * d_sl

            out.append(
                SignalEvent(
                    strategy="S1",
                    config_id=config.config_id,
                    symbol=symbol,
                    signal_ts=bar.close_ts,
                    side=side,
                    sl_distance_bps=d_sl * 1e4,
                    tp_distance_bps=d_tp * 1e4,
                    timeout_bars=_C.S1_TIMEOUT_1M_BARS,
                    cooldown_bars=_C.S1_COOLDOWN_1M_BARS,
                    fold_id=fold_id,
                )
            )

    result = tuple(out)
    _assert_unique_signal_ts(result)
    return result
