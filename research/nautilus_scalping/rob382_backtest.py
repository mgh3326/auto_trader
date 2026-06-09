"""ROB-382 — pure event-walk trade simulator for ported external signals (stdlib).

Mirrors freqtrade's split: a candidate module produces boolean ``entry``/``exit`` columns
(``populate_entry_trend`` / ``populate_exit_trend``), and THIS engine walks them into a
non-overlapping trade list the ROB-351 gate consumes. The split keeps ports honest:
signals are a causal function of OHLCV (no lookahead — column[i] uses bars[0..i]); the
engine owns fills.

Faithfulness rules (timeframe/hold-preserving — ROB-382 §2, not short-horizon coercion):
  * entry at bar close (the bar whose signal fired) — no lookahead.
  * exit by the strategy's OWN mechanism:
      - signal strategies (ichiV1/ElliotV7/ClucHAnix): exit on the published exit signal;
        a hard stop at the strategy's published stoploss bounds ruin; a generous max-hold
        bounds the spike. Hold is signal-driven → native horizon preserved.
      - roi/sl strategies with an empty exit signal (VWAP): exit at the strategy's PUBLISHED
        roi take-profit / stop (not hyperopted here) — that IS its exit. Recorded as
        exit_model so the verdict reads honestly.
  * SL-first within a bar (conservative): if a bar's range spans both stop and target,
    assume the stop filled first (matches strategy_meanrev's conservative exit).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import families  # make_taker_trade + REF_FEE_BPS (shared cost model)
from rob382_bars import OHLCVBar
from validated_gate import Trade

REF_FEE_BPS = families.REF_FEE_BPS
NOTIONAL = 1000.0


@dataclass(frozen=True)
class ExitModel:
    """How a ported strategy closes a long. ``type`` is 'signal' or 'roi_sl'."""

    type: str  # "signal" | "roi_sl"
    hard_sl_pct: float | None = None  # published stoploss magnitude (0.275 == -27.5%)
    roi_pct: float | None = None  # published take-profit (roi_sl only)
    max_hold_bars: int = 288  # generous time cap; recorded in the verdict


def simulate(
    bars: Sequence[OHLCVBar],
    entry: Sequence[bool],
    exit_sig: Sequence[bool],
    exit_model: ExitModel,
    *,
    notional: float = NOTIONAL,
    ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    """Walk causal entry/exit columns into non-overlapping long ``Trade``s.

    ``entry[i]``/``exit_sig[i]`` are aligned to ``bars`` and must be causal. Returns trades
    sorted by entry ts (they already are, by construction).
    """
    if not (len(bars) == len(entry) == len(exit_sig)):
        raise ValueError("bars/entry/exit_sig length mismatch")
    trades: list[Trade] = []
    n = len(bars)
    i = 0
    while i < n:
        if not entry[i]:
            i += 1
            continue
        entry_price = bars[i].close
        if entry_price <= 0:
            i += 1
            continue
        sl_price = (
            entry_price * (1.0 - exit_model.hard_sl_pct)
            if exit_model.hard_sl_pct is not None
            else None
        )
        tp_price = (
            entry_price * (1.0 + exit_model.roi_pct)
            if (exit_model.type == "roi_sl" and exit_model.roi_pct is not None)
            else None
        )
        exit_price = None
        exit_idx = None
        j = i + 1
        while j < n:
            bar = bars[j]
            # SL-first (conservative): stop fills before target within the same bar.
            if sl_price is not None and bar.low <= sl_price:
                exit_price, exit_idx = sl_price, j
                break
            if tp_price is not None and bar.high >= tp_price:
                exit_price, exit_idx = tp_price, j
                break
            if exit_model.type == "signal" and exit_sig[j]:
                exit_price, exit_idx = bar.close, j
                break
            if (j - i) >= exit_model.max_hold_bars:
                exit_price, exit_idx = bar.close, j
                break
            j += 1
        if exit_idx is None:  # ran off the end while in-position: close at last bar
            exit_price, exit_idx = bars[n - 1].close, n - 1
        ret = (exit_price - entry_price) / entry_price
        trades.append(
            families.make_taker_trade(ret * notional, bars[i].ts, notional, ref_fee_bps)
        )
        i = exit_idx + 1  # non-overlapping
    return trades


def avg_hold_bars(trades_with_idx: Sequence[tuple[int, int]]) -> float:
    """Diagnostic: mean (exit_idx - entry_idx) over trades, for the contrast table."""
    if not trades_with_idx:
        return 0.0
    return sum(e - s for s, e in trades_with_idx) / len(trades_with_idx)
