"""Idle cash. The export has no balances, so cash is bounded from the fills.

Assumptions, all stated in the report:

* No deposits or withdrawals. They are not in the export.
* Opening cash is the smallest amount that keeps cash at or above zero through the
  window, so measured cash is a lower bound.
* Cash earns 0%.
* Holdings the ledger never touched are invisible, which overstates the idle share.
* An implausible fill counts as held from the start. It has no purchase date and no
  cash outflow, so entering it later would read as free profit.
* A later seed is the whole holding at its date. The difference from the ledger is
  treated as an unrecorded trade at that day's close.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

from research.trade_retro.canonical import is_parking
from research.trade_retro.prices import PriceBook


def holding_path(
    rows: pd.DataFrame, cutover: datetime, later_seeds_reset: bool = True
) -> tuple[float, float, list[tuple[date, float]], list[tuple[date, float]]]:
    """One symbol's quantity through time.

    Returns the opening quantity, how much of it is inferred, the quantity after each
    event, and each later seed's change in quantity. A first-batch seed fixes the
    quantity at the cutover. A later seed sets the quantity to its own value, because
    it holds the whole position (see ``positions.py``). An implausible fill counts as
    held from the start. Anything still short implies a holding the ledger never saw.
    """
    ordered = rows.sort_values("ts", kind="stable")
    manual = (ordered["source"] == "manual_import").to_numpy()
    is_seed = ordered["seed"].to_numpy()
    stamps = ordered["ts"].to_numpy(dtype=object)
    first = is_seed & manual & np.array([t == cutover for t in stamps], dtype=bool)
    later = is_seed & manual & ~first
    signed = np.where(ordered["side"] == "buy", ordered["qty"], -ordered["qty"])
    before = ~is_seed & np.array([t < cutover for t in stamps], dtype=bool)

    opening = (
        max(float(signed[first].sum()) - float(signed[before].sum()), 0.0)
        if first.any()
        else 0.0
    )
    opening += float(signed[is_seed & ~manual].sum())
    if not later_seeds_reset:
        opening += float(signed[later].sum())

    def replay(
        start: float,
    ) -> tuple[float, list[tuple[date, float]], list[tuple[date, float]]]:
        quantity = low = start
        path: list[tuple[date, float]] = []
        changes: list[tuple[date, float]] = []
        for day, value, seed, reset in zip(
            ordered["date"], signed, is_seed, later, strict=True
        ):
            if reset and later_seeds_reset:
                changes.append((day, value - quantity))
                quantity = value
            elif not seed:
                quantity += value
            low = min(low, quantity)
            path.append((day, quantity))
        return low, path, changes

    low, _, _ = replay(opening)
    inferred = max(-low, 0.0)
    _, path, changes = replay(opening + inferred)
    return opening + inferred, inferred, path, changes


def opening_quantity(rows: pd.DataFrame, cutover: datetime) -> tuple[float, float]:
    """The quantity held before the first fill, and how much of it is inferred."""
    opening, inferred, _, _ = holding_path(rows, cutover)
    return opening, inferred


def daily_account(
    fills: pd.DataFrame,
    book: PriceBook,
    alias: str,
    market: str,
    cutover: datetime,
    later_seeds_reset: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """The account's daily cash, holdings and NAV, from its first fill to the last bar."""
    rows = fills[(fills["alias"] == alias) & (fills["market"] == market)]
    index = book.benchmarks[market]
    real = rows[~rows["seed"]]
    start = real["date"].min()
    i0 = index.idx_on_or_before(start)
    i0 = i0 if i0 is not None else 0
    grid = [index.date_at(i) for i in range(i0, len(index))]
    grid_ts = pd.to_datetime(grid)

    risk = np.zeros(len(grid))
    parking = np.zeros(len(grid))
    inferred_value = 0.0
    unpriced: list[str] = []
    reset_flow: dict[pd.Timestamp, float] = {}
    for symbol, group in rows.groupby("symbol", sort=True):
        opening, inferred, path, changes = holding_path(
            group, cutover, later_seeds_reset
        )
        held = np.full(len(grid), opening)
        for day, quantity in path:
            position = int(np.searchsorted(grid_ts, pd.Timestamp(day), side="left"))
            held[position:] = quantity
        held = np.clip(held, 0.0, None)

        bars = book.get(market, symbol)
        if bars is None:
            unpriced.append(symbol)
            continue
        closes = np.array(
            [
                float(bars.close[j])
                if (j := bars.idx_on_or_before(day)) is not None
                else np.nan
                for day in grid
            ]
        )
        closes = np.where(np.isnan(closes), bars.close[0], closes)
        value = held * closes
        if is_parking(market, symbol):
            parking += value
        else:
            risk += value
        inferred_value += float(inferred * closes[0])
        # A reset is an unrecorded trade. It goes through cash at that day's close, so
        # NAV neither jumps nor drops because the ledger was corrected.
        for day, change in changes:
            j = bars.idx_on_or_before(day)
            price = float(bars.close[j]) if j is not None else float(bars.close[0])
            stamp = pd.Timestamp(day)
            reset_flow[stamp] = reset_flow.get(stamp, 0.0) - change * price

    flow = np.where(real["side"] == "sell", 1.0, -1.0) * real["qty"] * real["price"]
    flow_by_day = (
        pd.Series(flow.to_numpy(), index=pd.to_datetime(real["date"]))
        .groupby(level=0)
        .sum()
    )
    if reset_flow:
        flow_by_day = flow_by_day.add(
            pd.Series(reset_flow), fill_value=0.0
        ).sort_index()
    before_flow = float(flow_by_day[flow_by_day.index < grid_ts[0]].sum())
    cumulative = (
        before_flow + flow_by_day.reindex(grid_ts, fill_value=0.0).cumsum().to_numpy()
    )
    opening_cash = max(-float(cumulative.min()), 0.0)
    cash = opening_cash + cumulative
    nav = cash + risk + parking
    index_close = np.array(
        [float(index.close[index.idx_on_or_before(d)]) for d in grid]
    )

    frame = pd.DataFrame(
        {
            "day": grid,
            "cash": cash,
            "risk": risk,
            "parking": parking,
            "nav": nav,
            "index": index_close,
        }
    )
    return frame, {
        "opening_cash_lower_bound": opening_cash,
        "inferred_opening_value": inferred_value,
        "unpriced_symbols": unpriced,
    }


def summarize_idle(frame: pd.DataFrame) -> dict:
    """Averages over days, and what the idle balance forwent at the index rate."""
    next_ret = frame["index"].shift(-1) / frame["index"] - 1.0
    live = frame["nav"] > 0
    cash_share = (frame["cash"] / frame["nav"]).where(live)
    idle_share = ((frame["cash"] + frame["parking"]) / frame["nav"]).where(live)
    forgone_cash = float((frame["cash"] * next_ret).sum())
    avg_cash = float(frame["cash"].mean())
    index_ret = float(frame["index"].iloc[-1] / frame["index"].iloc[0] - 1.0)
    first_nav = float(frame["nav"].iloc[0])
    nav_ret = (
        float(frame["nav"].iloc[-1]) / first_nav - 1.0
        if first_nav > 0
        else float("nan")
    )
    return {
        "days": len(frame),
        "start": frame["day"].iloc[0],
        "end": frame["day"].iloc[-1],
        "cash_share": float(cash_share.mean()),
        "idle_share": float(idle_share.mean()),
        "final_cash_share": float(cash_share.iloc[-1]),
        "final_idle_share": float(idle_share.iloc[-1]),
        "avg_cash": avg_cash,
        "avg_nav": float(frame["nav"].mean()),
        "forgone_cash": forgone_cash,
        "forgone_pct_of_avg_cash": forgone_cash / avg_cash
        if avg_cash
        else float("nan"),
        "forgone_pct_of_avg_nav": forgone_cash / float(frame["nav"].mean()),
        "index_return": index_ret,
        "nav_return": nav_ret,
    }
