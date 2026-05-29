"""ROB-351 (T7) — strategy-family signal generators (pure, stdlib).

Three bounded families produce the trade / period series the gate consumes:

  1. ``breakout_continuation_trades``  — single-symbol regime-agnostic range-
     expansion breakout + short hold (emits ``Trade``).
  2. ``ts_trend_basket_periods``       — per-symbol time-series trend sign,
     equal-weight basket (emits ``PortfolioPeriod`` for the portfolio gate).
  3. ``xs_momentum_periods``           — cross-sectional momentum, long top-k /
     short bottom-k, PIT-aware via ``panel`` (emits ``PortfolioPeriod``).

These implement the SIGNAL MECHANICS only. They are deliberately simple and
parameter-frozen; the empirical campaign RUN against Binance USDⓈ-M data (and
its verdict) is the operator's PR2 step — NO market data is committed. Costs use
the shared taker model; maker closability is handled downstream (maker_fill).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cost_model
import panel as _panel
from pit_universe import PITManifest
from validated_gate import PortfolioPeriod, Trade

REF_FEE_BPS = cost_model.REF_FEE_BPS


@dataclass(frozen=True)
class Bar:
    ts: int
    high: float
    low: float
    close: float


def make_taker_trade(
    gross_pnl: float, ts: int, notional: float, ref_fee_bps: float = REF_FEE_BPS
) -> Trade:
    """Build a gate ``Trade`` from a gross PnL with a 2-leg taker ref fee.

    ``net_ref_pnl`` is net at the reference fee; ``commission_ref`` is the 2-leg
    fee magnitude, so ``cost_model.net_at_fee(.., 0)`` recovers the gross PnL.
    """
    fee = 2.0 * ref_fee_bps / 1e4 * notional
    return Trade(net_ref_pnl=gross_pnl - fee, commission_ref=fee,
                 notional=notional, ts_opened=ts)


def _period_commission(turnover_notional: float, ref_fee_bps: float) -> float:
    """Ref-fee commission for a period given traded (turnover) notional."""
    return ref_fee_bps / 1e4 * turnover_notional


# --------------------------------------------------------------------------- #
# Family 1 — range-expansion breakout + short continuation hold (single symbol).
# --------------------------------------------------------------------------- #
def breakout_continuation_trades(
    bars: Sequence[Bar],
    lookback: int = 20,
    hold: int = 5,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
) -> list[Trade]:
    """Long when close breaks above the prior ``lookback`` high; exit after ``hold``.

    Non-overlapping entries (a position must close before the next entry). Gross
    PnL is the held close-to-close return on ``notional``.
    """
    trades: list[Trade] = []
    n = len(bars)
    i = lookback
    while i < n:
        prior_high = max(b.high for b in bars[i - lookback:i])
        if bars[i].close > prior_high:
            exit_idx = min(i + hold, n - 1)
            entry = bars[i].close
            ret = (bars[exit_idx].close - entry) / entry if entry else 0.0
            trades.append(make_taker_trade(ret * notional, bars[i].ts, notional, ref_fee_bps))
            i = exit_idx + 1  # non-overlapping
        else:
            i += 1
    return trades


# --------------------------------------------------------------------------- #
# Family 2 — time-series trend basket (equal-weight sign across symbols).
# --------------------------------------------------------------------------- #
def ts_trend_basket_periods(
    closes_by_symbol: dict[str, Sequence[tuple[int, float]]],
    lookback: int = 20,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
) -> list[PortfolioPeriod]:
    """Per symbol, hold sign(return over ``lookback``); equal-weight the basket.

    Each period's portfolio PnL is the mean over symbols of
    ``position * next-period return * per-symbol notional``. Turnover (position
    flips) is charged at the ref taker fee.
    """
    # align on the common chronological index by position (fixtures share a grid)
    symbols = sorted(closes_by_symbol)
    if not symbols:
        return []
    length = min(len(closes_by_symbol[s]) for s in symbols)
    per_symbol_notional = notional / len(symbols)
    prev_pos: dict[str, int] = dict.fromkeys(symbols, 0)
    periods: list[PortfolioPeriod] = []
    for t in range(lookback, length - 1):
        ts = closes_by_symbol[symbols[0]][t][0]
        gross = 0.0
        turnover = 0.0
        for s in symbols:
            series = closes_by_symbol[s]
            c_now = series[t][1]
            c_past = series[t - lookback][1]
            c_next = series[t + 1][1]
            pos = 1 if c_now > c_past else (-1 if c_now < c_past else 0)
            ret = (c_next - c_now) / c_now if c_now else 0.0
            gross += pos * ret * per_symbol_notional
            if pos != prev_pos[s]:
                turnover += per_symbol_notional
            prev_pos[s] = pos
        periods.append(PortfolioPeriod(
            ts=ts, gross_ref_pnl=gross - _period_commission(turnover, ref_fee_bps),
            commission_ref=_period_commission(turnover, ref_fee_bps)))
    return periods


# --------------------------------------------------------------------------- #
# Family 3 — cross-sectional momentum (long top-k / short bottom-k), PIT-aware.
# --------------------------------------------------------------------------- #
def xs_momentum_periods(
    closes_by_symbol: dict[str, Sequence[tuple[int, float]]],
    rebalances: Sequence[int],
    lookback: int = 20,
    top_k: int = 1,
    notional: float = 1000.0,
    ref_fee_bps: float = REF_FEE_BPS,
    manifest: PITManifest | None = None,
    min_seasoning: int = 0,
) -> list[PortfolioPeriod]:
    """Rank lookback returns at each rebalance; long top-k, short bottom-k.

    Realizes each leg's return to the NEXT rebalance. Universe is taken as-of each
    rebalance via the PIT panel (Codex hardening). Costs charged on the rebalanced
    notional (full turnover each rebalance).
    """
    rebals = sorted(rebalances)
    xs_by_ts = dict(_panel.iter_rebalance_cross_sections(
        closes_by_symbol, rebals, lookback, manifest=manifest, min_seasoning=min_seasoning))
    periods: list[PortfolioPeriod] = []
    for idx, ts in enumerate(rebals[:-1]):
        xs = xs_by_ts.get(ts, {})
        if len(xs) < 2 * top_k:
            continue
        ranked = sorted(xs, key=xs.get, reverse=True)
        longs, shorts = ranked[:top_k], ranked[-top_k:]
        next_ts = rebals[idx + 1]
        leg_notional = notional / (2 * top_k)
        gross = 0.0
        for sym in longs:
            gross += _realized_return(closes_by_symbol[sym], ts, next_ts) * leg_notional
        for sym in shorts:
            gross -= _realized_return(closes_by_symbol[sym], ts, next_ts) * leg_notional
        turnover = notional  # full rebalance
        periods.append(PortfolioPeriod(
            ts=ts, gross_ref_pnl=gross - _period_commission(turnover, ref_fee_bps),
            commission_ref=_period_commission(turnover, ref_fee_bps)))
    return periods


def _realized_return(series: Sequence[tuple[int, float]], ts0: int, ts1: int) -> float:
    c0 = _panel._close_at_or_before(series, ts0)
    c1 = _panel._close_at_or_before(series, ts1)
    if c0 is None or c1 is None or c0 == 0.0:
        return 0.0
    return c1 / c0 - 1.0
