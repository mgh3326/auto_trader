"""Each position's quantity and average cost, day by day.

The rules in ``buy.underwater_support_net`` and ``sell.loss_cut`` read a lot's
unrealized P&L against its average cost, the way the broker shows it, not against
first-in-first-out lots. This module rebuilds that view from the canonical fills
of ``research.trade_retro``:

* A buy moves the average cost to the weighted mean. A sell leaves it unchanged.
* A seed row (``manual_import``) is the broker's whole holding at its stamp, at
  the broker's average cost, so it resets quantity and cost. That holds for the
  first batch, which already contains every earlier fill, and for a later batch,
  which restates the holding (see ``research.trade_retro.positions``).
* An implausible fill (an old average cost booked as a fill) is added like a buy
  at that price.
* A sell of shares the ledger never saw proves older holdings with no known cost.
  While any of them remain, sold oldest first, the cost is unknown and the day is
  left out.

The result is one row per symbol and bar date from the first event to the last
bar, with ``qty``, ``avg``, ``close`` and ``known``. ``episode`` counts the
returns to flat, so one episode runs from flat back to flat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from research.trade_retro.canonical import is_parking
from research.trade_retro.prices import Bars, PriceBook

EPS = 1e-9


@dataclass
class State:
    qty: float = 0.0
    avg: float = float("nan")
    inferred: float = 0.0
    episode: int = 0
    started_by_seed: bool = False

    @property
    def known(self) -> bool:
        return self.qty > EPS and self.inferred <= EPS and self.avg == self.avg


def _ordered(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.assign(
        _rank=[0 if s == "buy" else 1 for s in rows["side"]]
    ).sort_values(["ts", "_rank"], kind="stable")


def _is_reset(row, cutover: datetime) -> bool:
    return bool(row.seed) and row.source == "manual_import" and row.ts >= cutover


def inferred_opening(rows: pd.DataFrame, cutover: datetime) -> float:
    """Shares that must have been held before the first recorded event."""
    balance = low = 0.0
    for row in _ordered(rows).itertuples(index=False):
        if _is_reset(row, cutover):
            balance = row.qty
        elif row.side == "sell":
            balance -= row.qty
        else:
            balance += row.qty
        low = min(low, balance)
    return max(-low, 0.0)


def apply(state: State, row, cutover: datetime) -> State:
    """Apply one fill to the state. Returns the same object, changed."""
    was_flat = state.qty + state.inferred <= EPS
    if _is_reset(row, cutover):
        if was_flat:
            state.episode += 1
            state.started_by_seed = True
        state.qty, state.avg, state.inferred = float(row.qty), float(row.price), 0.0
    elif row.side == "buy":
        if was_flat:
            state.episode += 1
            state.started_by_seed = bool(row.seed)
            state.avg = float(row.price)
            state.qty = float(row.qty)
        else:
            total = state.qty + row.qty
            if state.qty > EPS and state.avg == state.avg:
                state.avg = (state.qty * state.avg + row.qty * row.price) / total
            elif state.qty <= EPS:
                state.avg = float(row.price)
            state.qty = total
    else:
        remaining = float(row.qty)
        taken = min(state.inferred, remaining)
        state.inferred -= taken
        remaining -= taken
        state.qty = max(state.qty - remaining, 0.0)
    if state.qty + state.inferred <= EPS:
        state.qty, state.inferred, state.avg = 0.0, 0.0, float("nan")
    return state


def symbol_path(rows: pd.DataFrame, bars: Bars, cutover: datetime) -> pd.DataFrame:
    """Daily state for one account and symbol, from its first event to the last bar.

    A fill counts on its session date, and a fill on a day without a bar lands on
    the next bar. The state is as of that day's close.
    """
    ordered = _ordered(rows)
    state = State(inferred=inferred_opening(rows, cutover))
    events = list(ordered.itertuples(index=False))
    start = bars.idx_on_or_after(events[0].date)
    if start is None:
        return pd.DataFrame()
    out = []
    k = 0
    for i in range(start, len(bars)):
        day = bars.date_at(i)
        while k < len(events) and events[k].date <= day:
            apply(state, events[k], cutover)
            k += 1
        out.append(
            {
                "i": i,
                "day": day,
                "close": float(bars.close[i]),
                "qty": state.qty,
                "avg": state.avg,
                "known": state.known,
                "episode": state.episode,
                "started_by_seed": state.started_by_seed,
            }
        )
    frame = pd.DataFrame(out)
    frame["unrealized"] = np.where(
        frame["known"], frame["close"] / frame["avg"] - 1.0, np.nan
    )
    return frame


def all_paths(
    fills: pd.DataFrame, book: PriceBook, cutover: datetime, parking: bool = False
) -> pd.DataFrame:
    """Daily state for every account and symbol with bars. Parking is left out unless
    asked for, since a cash-like holding is not what the averaging and cut rules mean."""
    frames = []
    for (alias, market, symbol), rows in fills.groupby(
        ["alias", "market", "symbol"], sort=True
    ):
        if is_parking(market, symbol) != parking:
            continue
        bars = book.get(market, symbol)
        if bars is None or not len(bars):
            continue
        path = symbol_path(rows, bars, cutover)
        if path.empty:
            continue
        path.insert(0, "symbol", symbol)
        path.insert(0, "market", market)
        path.insert(0, "alias", alias)
        frames.append(path)
    return pd.concat(frames, ignore_index=True)


def state_before(
    rows: pd.DataFrame, cutover: datetime, stamp: datetime, key: str | None = None
) -> State:
    """The state just before a fill: every event earlier than ``stamp``, and none of
    the fill's own rows when ``key`` is given."""
    state = State(inferred=inferred_opening(rows, cutover))
    for row in _ordered(rows).itertuples(index=False):
        if row.ts > stamp or (key is not None and row.key == key):
            continue
        if row.ts == stamp and row.side == "buy" and key is None:
            continue
        apply(state, row, cutover)
    return state
