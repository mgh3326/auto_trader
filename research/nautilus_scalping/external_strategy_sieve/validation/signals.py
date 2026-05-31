"""ROB-383 Phase 3 - clean-room signals to validated_gate Trades.

Each signal turns a bar series into non-overlapping round-trip trades. Gross PnL
is the realized close-to-close return on a fixed notional, recorded at
``REF_FEE_BPS`` via ``families.make_taker_trade`` so ``cost_model`` rescales to
any fee. Signals are clean-room reimplementations of public indicator concepts.
"""

from __future__ import annotations

from collections.abc import Sequence

import families
from families import REF_FEE_BPS, make_taker_trade
from validated_gate import Trade

from external_strategy_sieve.validation.indicators import atr, bollinger, closes_of, rsi


def _round_trip(
    direction: str,
    entry_close: float,
    exit_close: float,
    ts: int,
    notional: float,
    ref_fee_bps: float,
) -> Trade | None:
    if entry_close <= 0:
        return None
    ret = (exit_close - entry_close) / entry_close
    if direction == "short":
        ret = -ret
    return make_taker_trade(ret * notional, ts, notional, ref_fee_bps)


def _trades_from_direction(
    bars: Sequence[families.Bar],
    direction: list[str | None],
    notional: float,
    ref_fee_bps: float,
) -> list[Trade]:
    """Open on first direction and realize a trade on each direction change."""
    trades: list[Trade] = []
    pos: tuple[str, float, int] | None = None
    for i, d in enumerate(direction):
        if d is None:
            continue
        if pos is None:
            pos = (d, bars[i].close, bars[i].ts)
            continue
        if d != pos[0]:
            trade = _round_trip(
                pos[0], pos[1], bars[i].close, pos[2], notional, ref_fee_bps
            )
            if trade:
                trades.append(trade)
            pos = (d, bars[i].close, bars[i].ts)
    return trades


def supertrend_trades(
    bars: Sequence[families.Bar],
    atr_period: int = 10,
    multiplier: float = 3.0,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    a = atr(bars, atr_period)
    direction: list[str | None] = [None] * len(bars)
    fu_prev = fl_prev = None
    prev_dir = "long"
    for i in range(len(bars)):
        if a[i] is None:
            continue
        hl2 = (bars[i].high + bars[i].low) / 2.0
        basic_upper = hl2 + multiplier * a[i]
        basic_lower = hl2 - multiplier * a[i]
        if fu_prev is None:
            fu_prev, fl_prev = basic_upper, basic_lower
            direction[i] = prev_dir
            continue
        c_prev = bars[i - 1].close
        final_upper = basic_upper if basic_upper < fu_prev or c_prev > fu_prev else fu_prev
        final_lower = basic_lower if basic_lower > fl_prev or c_prev < fl_prev else fl_prev
        close = bars[i].close
        if close > fu_prev:
            next_dir = "long"
        elif close < fl_prev:
            next_dir = "short"
        else:
            next_dir = prev_dir
        direction[i] = next_dir
        fu_prev, fl_prev, prev_dir = final_upper, final_lower, next_dir
    return _trades_from_direction(bars, direction, notional, ref_fee_bps)


def chandelier_trades(
    bars: Sequence[families.Bar],
    atr_period: int = 22,
    multiplier: float = 3.0,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    a = atr(bars, atr_period)
    direction: list[str | None] = [None] * len(bars)
    prev_dir = "long"
    for i in range(len(bars)):
        if a[i] is None or i < atr_period:
            continue
        window = bars[i - atr_period + 1 : i + 1]
        highest_high = max(b.high for b in window)
        lowest_low = min(b.low for b in window)
        long_stop = highest_high - multiplier * a[i]
        short_stop = lowest_low + multiplier * a[i]
        close = bars[i].close
        if close > short_stop:
            next_dir = "long"
        elif close < long_stop:
            next_dir = "short"
        else:
            next_dir = prev_dir
        direction[i] = next_dir
        prev_dir = next_dir
    return _trades_from_direction(bars, direction, notional, ref_fee_bps)


def bbrsi_trades(
    bars: Sequence[families.Bar],
    bb_period: int = 20,
    bb_k: float = 2.0,
    rsi_period: int = 14,
    rsi_oversold: float = 30.0,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    """Long-only mean reversion: lower Bollinger breach plus RSI oversold."""
    closes = closes_of(bars)
    mid, _upper, lower = bollinger(closes, bb_period, bb_k)
    r = rsi(closes, rsi_period)
    trades: list[Trade] = []
    pos: tuple[float, int] | None = None
    for i in range(len(bars)):
        if lower[i] is None or r[i] is None or mid[i] is None:
            continue
        close = bars[i].close
        if pos is None:
            if close < lower[i] and r[i] < rsi_oversold:
                pos = (close, bars[i].ts)
        elif close >= mid[i]:
            trade = _round_trip("long", pos[0], close, pos[1], notional, ref_fee_bps)
            if trade:
                trades.append(trade)
            pos = None
    return trades
