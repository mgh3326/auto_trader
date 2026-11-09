"""Causal panel: wide price frames, features at decision close, open-to-open outcomes.

Decision at the close of session i. Entry = open of session i+1, exit = open of
session i+1+h (an h-session hold). A name whose series ended early (last bar
more than 7 days before the panel end) exits at its last close instead: that is
a recorded terminal convention, not a payoff claim. A gap inside a live series
leaves the outcome unknown (NaN), never filled.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import spec

TERMINAL_DAYS = 7


@dataclass
class Panel:
    market: str
    dates: pd.DatetimeIndex
    symbols: list[str]
    open: pd.DataFrame
    close: pd.DataFrame
    high: pd.DataFrame
    volume: pd.DataFrame
    traded_value: pd.DataFrame
    bench_open: pd.Series
    last_idx: np.ndarray  # index of each symbol's last bar in ``dates``
    terminated: np.ndarray  # bool: series ended before the panel end

    @property
    def n(self) -> int:
        return len(self.dates)


def build_panel(
    market: str, frames: dict, bench: pd.DataFrame, *, start: str, end: str
) -> Panel:
    """``frames``: symbol -> frame indexed by date with BAR_COLUMNS. Benchmark sets the calendar."""
    days = bench.index[
        (bench.index >= pd.Timestamp(start)) & (bench.index <= pd.Timestamp(end))
    ]
    days = pd.DatetimeIndex(sorted(days))
    if days.empty or bench.reindex(days).open.isna().any():
        raise ValueError("benchmark must cover every session of the calendar")
    symbols = sorted(frames)

    def wide(col: str) -> pd.DataFrame:
        return pd.DataFrame(
            {s: frames[s][col].reindex(days) for s in symbols}, index=days
        )

    open_, close, high = wide("open"), wide("close"), wide("high")
    volume, traded = wide("volume"), wide("traded_value")
    bad = (open_ <= 0) | (close <= 0) | (high <= 0)
    open_, close, high = open_.mask(bad), close.mask(bad), high.mask(bad)
    last_idx = np.array(
        [
            int(np.flatnonzero(close[s].notna().to_numpy())[-1])
            if close[s].notna().any()
            else -1
            for s in symbols
        ]
    )
    cutoff = days[-1] - pd.Timedelta(days=TERMINAL_DAYS)
    terminated = np.array([i >= 0 and days[i] < cutoff for i in last_idx], dtype=bool)
    return Panel(
        market, days, symbols, open_, close, high, volume, traded,
        bench.open.reindex(days), last_idx, terminated,
    )  # fmt: skip


def features(panel: Panel) -> dict[str, np.ndarray]:
    """Score matrices (T x M), higher = long. NaN where history is incomplete."""
    close, high, volume = panel.close, panel.high, panel.volume
    ret1 = close.pct_change(fill_method=None)
    window = spec.HIGH_WINDOW[panel.market]
    out = {
        "momentum_20": close.pct_change(20, fill_method=None),
        "momentum_60": close.pct_change(60, fill_method=None),
        "reversal_5": -close.pct_change(5, fill_method=None),
        "reversal_1": -ret1,
        "low_volatility_20": -ret1.rolling(20).std(ddof=1),
        "high_distance_52w": close / high.rolling(window).max() - 1,
        "volume_shock_20": volume
        / volume.shift(1).rolling(20).mean().replace(0, np.nan)
        - 1,
    }
    return {k: v.replace([np.inf, -np.inf], np.nan).to_numpy() for k, v in out.items()}


def eligibility(panel: Panel, feats: dict[str, np.ndarray]) -> np.ndarray:
    """Top-N liquid among names with every feature known and a positive-volume bar today."""
    ready = np.ones(panel.close.shape, dtype=bool)
    for score in feats.values():
        ready &= np.isfinite(score)
    ready &= panel.volume.to_numpy() > 0
    liquidity = panel.traded_value.rolling(20).mean().to_numpy()
    ready &= np.isfinite(liquidity)
    masked = np.where(ready, liquidity, -np.inf)
    # rank 1 = most liquid; ties by column (= symbol) order
    order = np.argsort(-masked, axis=1, kind="stable")
    rank = np.empty_like(order)
    rows = np.arange(order.shape[0])[:, None]
    rank[rows, order] = np.arange(order.shape[1])[None, :]
    return ready & (rank < spec.LIQUID_N[panel.market])


def outcomes(panel: Panel, horizon: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (asset open-to-open return TxM, benchmark return T, exit date index T).

    Row i is the decision at close of session i; NaN when the hold runs past the
    panel or an endpoint is missing. ``exit_idx`` is -1 where the hold is not
    complete, so a window can require the exit inside it.
    """
    n, m = panel.n, len(panel.symbols)
    op, cl = panel.open.to_numpy(), panel.close.to_numpy()
    ret = np.full((n, m), np.nan)
    bench = np.full(n, np.nan)
    exit_idx = np.full(n, -1)
    bopen = panel.bench_open.to_numpy()
    for i in range(n - 1 - horizon):
        entry_i, exit_i = i + 1, i + 1 + horizon
        entry = op[entry_i]
        exit_px = op[exit_i].copy()
        ended = (
            panel.terminated & (panel.last_idx < exit_i) & (panel.last_idx >= entry_i)
        )
        if ended.any():
            terminal = cl[np.clip(panel.last_idx, 0, n - 1), np.arange(m)]
            exit_px = np.where(ended, terminal, exit_px)
        with np.errstate(invalid="ignore", divide="ignore"):
            ret[i] = exit_px / entry - 1
        bench[i] = bopen[exit_i] / bopen[entry_i] - 1
        exit_idx[i] = exit_i
    ret[~np.isfinite(ret)] = np.nan
    return ret, bench, exit_idx
