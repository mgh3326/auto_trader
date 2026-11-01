"""Markouts: what happened after an execution, in sessions of the symbol's own bars."""

from __future__ import annotations

from datetime import date

import numpy as np

from research.trade_retro.prices import Bars, PriceBook

HORIZONS = (1, 5, 20)


def markout(
    bars: Bars, fill_day: date, fill_price: float, side: str, horizon: int
) -> float | None:
    """Signed return from the fill price to the close ``horizon`` sessions later.

    Session 0 is the first bar on or after the fill day. A positive value means the
    trade looked right: a buy that rose or a sell that fell. ``None`` means the
    horizon is past the last bar (censored), never zero.
    """
    i = bars.idx_on_or_after(fill_day)
    if i is None or i + horizon >= len(bars):
        return None
    move = float(bars.close[i + horizon]) / fill_price - 1.0
    return move if side == "buy" else -move


def index_excess(
    value: float | None,
    book: PriceBook,
    market: str,
    bars: Bars,
    fill_day: date,
    side: str,
    horizon: int,
) -> float | None:
    """The markout minus the benchmark's move over the same dates.

    A sell's excess is the benchmark move minus the stock's move, which is the gain
    from having moved out of the stock and into the index.
    """
    if value is None:
        return None
    i = bars.idx_on_or_after(fill_day)
    start, end = bars.date_at(i), bars.date_at(i + horizon)
    index_ret = book.index_return(market, start, end)
    if index_ret is None:
        return None
    return value - index_ret if side == "buy" else value + index_ret


def cluster_ci(
    values: np.ndarray,
    clusters: np.ndarray,
    reps: int = 2000,
    seed: int = 1439,
) -> tuple[float, float] | None:
    """95% percentile bootstrap of the mean, resampling symbols. Needs 3+ symbols."""
    names = np.unique(clusters)
    if len(names) < 3:
        return None
    groups = [values[clusters == name] for name in names]
    sums = np.array([g.sum() for g in groups])
    counts = np.array([len(g) for g in groups])
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(names), size=(reps, len(names)))
    means = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
