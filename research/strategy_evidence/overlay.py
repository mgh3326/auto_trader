"""Question 6: idle cash, and how each candidate rule would have moved it.

The base is the retro's daily account (``research.trade_retro.idle.daily_account``):
cash is a lower bound derived from fills with no external flows, and NAV is cash plus
holdings at the close. A rule is applied as an *overlay*: a list of extra trades
(positive quantity buys, negative sells) on top of the real ones. For each day

    cash' = cash - sum(qty x price of overlay trades so far)
    NAV'  = NAV + (cash' - cash) + sum(overlay quantity held x close)

A buy rule can push ``cash'`` below zero, which the lower-bound cash cannot pay for.
That shortfall is reported as extra cash needed, and the cash share is then measured
with that amount added from the first day, which is the least cash the rule needs.
Drawdown is the largest fall of NAV from its running peak, so it includes the real
book's own drawdown.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.trade_retro.idle import daily_account
from research.trade_retro.prices import PriceBook


def account_frames(fills: pd.DataFrame, book: PriceBook, cutover) -> dict:
    frames = {}
    for (alias, market), _ in fills.groupby(["alias", "market"], sort=True):
        frame, _meta = daily_account(fills, book, alias, market, cutover)
        frames[(alias, market)] = frame
    return frames


def max_drawdown(nav: np.ndarray) -> float:
    peak = np.maximum.accumulate(nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, nav / peak - 1.0, 0.0)
    return float(dd.min())


def apply_overlay(
    frame: pd.DataFrame, trades: pd.DataFrame, book: PriceBook, market: str
) -> dict:
    """Metrics of one account with the overlay trades added."""
    grid = pd.to_datetime(frame["day"]).to_numpy()
    delta_cash = np.zeros(len(frame))
    delta_hold = np.zeros(len(frame))
    for symbol, group in trades.groupby("symbol", sort=True):
        bars = book.get(market, symbol)
        closes = np.array(
            [
                float(bars.close[j])
                if (j := bars.idx_on_or_before(d)) is not None
                else float(bars.close[0])
                for d in frame["day"]
            ]
        )
        held = np.zeros(len(frame))
        for row in group.itertuples(index=False):
            k = int(np.searchsorted(grid, np.datetime64(row.day), side="left"))
            if k >= len(frame):
                continue
            delta_cash[k:] -= row.qty * row.price
            held[k:] += row.qty
        delta_hold += held * closes
    cash = frame["cash"].to_numpy() + delta_cash
    extra = max(0.0, -float(cash.min()))
    cash = cash + extra
    nav_base = frame["nav"].to_numpy()
    nav = nav_base + delta_cash + delta_hold + extra
    live = nav > 0
    avg_nav = float(nav_base.mean())
    return {
        "days": len(frame),
        "cash_share": float(np.mean(cash[live] / nav[live])),
        "idle_share": float(
            np.mean((cash[live] + frame["parking"].to_numpy()[live]) / nav[live])
        ),
        "extra_cash_pct_avg_nav": extra / avg_nav if avg_nav else float("nan"),
        "pnl_change_pct_avg_nav": float((delta_cash[-1] + delta_hold[-1]) / avg_nav)
        if avg_nav
        else float("nan"),
        "max_drawdown": max_drawdown(nav),
    }


def market_overlay(
    frames: dict, trades: pd.DataFrame, book: PriceBook, market: str
) -> pd.DataFrame:
    rows = []
    for (alias, m), frame in sorted(frames.items()):
        if m != market:
            continue
        sub = (
            trades[(trades["alias"] == alias) & (trades["market"] == market)]
            if len(trades)
            else trades
        )
        rows.append({"alias": alias, **apply_overlay(frame, sub, book, market)})
    return pd.DataFrame(rows)


EMPTY = pd.DataFrame(columns=["alias", "market", "symbol", "day", "qty", "price"])


def parking_sweep(
    frame: pd.DataFrame,
    annual_yield: float,
    idle_yield: float,
    round_trip: float,
    buffer: float = 0.10,
) -> dict:
    """Cash above ``buffer`` x NAV held in the parking instrument instead.

    The gain is the excess cash times the yield above idle cash, by calendar day.
    Costs bracket the turnover: ``once`` pays one round trip on the largest excess;
    ``monthly`` pays one round trip every 30 days on the average excess.
    """
    days = pd.to_datetime(frame["day"])
    gaps = days.shift(-1).sub(days).dt.days.fillna(1).to_numpy()
    cash = frame["cash"].to_numpy()
    nav = frame["nav"].to_numpy()
    excess = np.clip(cash - buffer * nav, 0.0, None)
    gain = float((excess * gaps).sum() * (annual_yield - idle_yield) / 365.0)
    avg_nav = float(nav.mean())
    span = float(gaps.sum())
    once = round_trip * float(excess.max())
    monthly = round_trip * float(excess.mean()) * span / 30.0
    live = nav > 0
    return {
        "excess_share": float(excess.mean() / avg_nav),
        "cash_share_after": float(np.mean((cash - excess)[live] / nav[live])),
        "gross_pct_avg_nav": gain / avg_nav,
        "net_once_pct_avg_nav": (gain - once) / avg_nav,
        "net_monthly_pct_avg_nav": (gain - monthly) / avg_nav,
    }
