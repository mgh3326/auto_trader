"""Idle cash. The export has no balances, so cash is bounded from the fills.

Assumptions, all stated in the report:

* No deposits or withdrawals. They are not in the export.
* Opening cash is the smallest amount that keeps cash at or above zero through the
  window, so measured cash is a lower bound.
* Cash earns 0%.
* Holdings the ledger never touched are invisible, which overstates the idle share.
* A seed row or implausible fill counts as held from the start. It has no purchase
  date and no cash outflow, so entering it later would read as free profit.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from research.trade_retro.canonical import is_parking
from research.trade_retro.prices import PriceBook


def opening_quantity(rows: pd.DataFrame, cutover: datetime) -> tuple[float, float]:
    """The quantity held before the first fill, and how much of it is inferred.

    A first-batch seed fixes the quantity at the cutover, and every other seed row is
    added to it. Anything still short, a sell with nothing recorded to sell, implies a
    holding the ledger never saw.
    """
    ordered = rows.sort_values("ts", kind="stable")
    signed = np.where(ordered["side"] == "buy", ordered["qty"], -ordered["qty"])
    is_seed = ordered["seed"].to_numpy()
    first_seed = (
        (ordered["source"] == "manual_import") & (ordered["ts"] == cutover)
    ).to_numpy()
    before_cutover = ~is_seed & (ordered["ts"] < cutover).to_numpy()
    opening = (
        max(float(signed[first_seed].sum()) - float(signed[before_cutover].sum()), 0.0)
        if first_seed.any()
        else 0.0
    )
    opening += float(signed[is_seed & ~first_seed].sum())

    path = opening
    low = opening
    for value in signed[~is_seed]:
        path += value
        low = min(low, path)
    inferred = max(-low, 0.0)
    return opening + inferred, inferred


def daily_account(
    fills: pd.DataFrame,
    book: PriceBook,
    alias: str,
    market: str,
    cutover: datetime,
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
    for symbol, group in rows.groupby("symbol", sort=True):
        opening, inferred = opening_quantity(group, cutover)
        ordered = group[~group["seed"]].sort_values("ts", kind="stable")
        signed = np.where(ordered["side"] == "buy", ordered["qty"], -ordered["qty"])
        delta = (
            pd.Series(signed, index=pd.to_datetime(ordered["date"]))
            .groupby(level=0)
            .sum()
        )
        before = float(delta[delta.index < grid_ts[0]].sum())
        held = (
            opening
            + before
            + delta.reindex(grid_ts, fill_value=0.0).cumsum().to_numpy()
        )
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

    flow = np.where(real["side"] == "sell", 1.0, -1.0) * real["qty"] * real["price"]
    flow_by_day = (
        pd.Series(flow.to_numpy(), index=pd.to_datetime(real["date"]))
        .groupby(level=0)
        .sum()
    )
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
