"""Question 2: what selling at the first touch of -X% would have done.

For every position episode (flat back to flat, one account and symbol) and every
threshold X, the touch is the first session whose close is at or below average cost
x (1 - X). Daily bars hold only open and close, so an intraday touch that recovered
by the close is not seen, and the cut is priced at that close.

At the touch the position holds Q shares at close P. Two outcomes are compared at
D+20, D+60 and the last bar:

* ``hold``: the close at the horizon against P. What the shares themselves did.
* ``actual``: what really became of those Q shares. Later sells consume them first,
  at their real prices, and whatever is left is marked at the horizon close. Later
  buys are other decisions (question 1) and are not part of it.

A cut earns 0% from P before its sell-leg cost, so ``cut minus actual`` is
``-actual`` less one leg of ``ROUND_TRIP_COST``. A touch on the first observed day of
an episode (an opening lot already below -X%) is reported apart from a crossing seen
in the window, because there the rule could not have fired at -X% itself.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.strategy_evidence.common import ROUND_TRIP_COST, TOLERANCE
from research.trade_retro.prices import PriceBook

THRESHOLDS = (0.08, 0.12, 0.15, 0.20, 0.30, 0.40)
HORIZONS = (("D+20", 20), ("D+60", 60), ("end", None))
TOUCH_COLUMNS = [
    "market", "alias", "symbol", "episode", "threshold", "crossed", "touch_day", "horizon",
    "notional", "hold", "actual", "cut_minus_actual", "sold_share", "worst_after", "sessions",
]  # fmt: skip
SUMMARY_COLUMNS = [
    "market", "threshold", "crossed", "horizon", "n", "symbols", "hold_median", "hold_mean",
    "actual_median", "cut_better", "cut_minus_actual_wtd", "cut_minus_actual_mean",
    "worst_after_median", "sold_share_mean",
]  # fmt: skip


@dataclass(frozen=True)
class Touch:
    alias: str
    market: str
    symbol: str
    episode: int
    threshold: float
    i: int
    day: object
    qty: float
    price: float
    crossed: bool


def first_touches(paths: pd.DataFrame, threshold: float) -> list[Touch]:
    """The first close at or below -``threshold`` in each episode with a known cost."""
    out: list[Touch] = []
    known = paths[paths["known"] & (paths["qty"] > 0)]
    for (alias, market, symbol, episode), group in known.groupby(
        ["alias", "market", "symbol", "episode"], sort=True
    ):
        hit = group[group["unrealized"] <= -threshold + TOLERANCE]
        if hit.empty:
            continue
        first = hit.iloc[0]
        out.append(
            Touch(
                alias,
                market,
                symbol,
                int(episode),
                threshold,
                int(first["i"]),
                first["day"],
                float(first["qty"]),
                float(first["close"]),
                crossed=bool(first["i"] != group["i"].iloc[0]),
            )
        )
    return out


def consumed_sells(
    touch: Touch, sells: pd.DataFrame, end_day
) -> list[tuple[object, float, float]]:
    """Sells after the touch session, up to ``end_day``, taken from the touch's Q
    shares first. Returns (day, quantity, price)."""
    remaining = touch.qty
    out = []
    window = sells[(sells["date"] > touch.day) & (sells["date"] <= end_day)]
    for row in window.sort_values("ts", kind="stable").itertuples(index=False):
        if remaining <= 1e-12:
            break
        taken = min(remaining, float(row.qty))
        out.append((row.date, taken, float(row.price)))
        remaining -= taken
    return out


def evaluate(
    touch: Touch, bars, sells: pd.DataFrame, horizon: int | None
) -> dict | None:
    j = len(bars) - 1 if horizon is None else touch.i + horizon
    if j >= len(bars):
        return None
    end_day = bars.date_at(j)
    consumed = consumed_sells(touch, sells, end_day)
    sold = sum(q for _, q, _ in consumed)
    proceeds = sum(q * p for _, q, p in consumed)
    close = float(bars.close[j])
    actual_value = proceeds + (touch.qty - sold) * close
    base = touch.qty * touch.price
    leg = ROUND_TRIP_COST[touch.market] / 2
    actual = actual_value / base - 1.0
    return {
        "hold": close / touch.price - 1.0,
        "actual": actual,
        "cut_minus_actual": -leg - actual,
        "sold_share": sold / touch.qty,
        "worst_after": float(bars.close[touch.i : j + 1].min()) / touch.price - 1.0,
        "sessions": j - touch.i,
    }


def losscut_table(
    paths: pd.DataFrame, fills: pd.DataFrame, book: PriceBook
) -> pd.DataFrame:
    """One row per touch and horizon."""
    sells = fills[(fills["side"] == "sell") & ~fills["seed"]]
    sells_by = dict(list(sells.groupby(["alias", "market", "symbol"], sort=True)))
    empty = fills.iloc[0:0]
    rows = []
    for threshold in THRESHOLDS:
        for touch in first_touches(paths, threshold):
            bars = book.get(touch.market, touch.symbol)
            sells = sells_by.get((touch.alias, touch.market, touch.symbol), empty)
            for name, horizon in HORIZONS:
                result = evaluate(touch, bars, sells, horizon)
                if result is None:
                    continue
                rows.append(
                    {
                        "market": touch.market,
                        "alias": touch.alias,
                        "symbol": touch.symbol,
                        "episode": touch.episode,
                        "threshold": threshold,
                        "crossed": touch.crossed,
                        "touch_day": touch.day,
                        "horizon": name,
                        "notional": touch.qty * touch.price,
                        **result,
                    }
                )
    return pd.DataFrame(rows, columns=TOUCH_COLUMNS)


def overlay_trades(
    paths: pd.DataFrame, fills: pd.DataFrame, book: PriceBook, threshold: float
) -> pd.DataFrame:
    """The loss-cut rule as changes to the real book, for the cash and NAV overlay:
    sell Q at the touch close, and undo the later real sells of those shares."""
    sells_all = fills[(fills["side"] == "sell") & ~fills["seed"]]
    rows = []
    for touch in first_touches(paths, threshold):
        bars = book.get(touch.market, touch.symbol)
        sells = sells_all[
            (sells_all["alias"] == touch.alias)
            & (sells_all["market"] == touch.market)
            & (sells_all["symbol"] == touch.symbol)
        ]
        base = {"alias": touch.alias, "market": touch.market, "symbol": touch.symbol}
        rows.append({**base, "day": touch.day, "qty": -touch.qty, "price": touch.price})
        for day, qty, price in consumed_sells(
            touch, sells, bars.date_at(len(bars) - 1)
        ):
            rows.append({**base, "day": day, "qty": qty, "price": price})
    return pd.DataFrame(
        rows, columns=["alias", "market", "symbol", "day", "qty", "price"]
    )


def summarize_losscut(table: pd.DataFrame) -> pd.DataFrame:
    """Per market, threshold, crossing group and horizon."""
    rows = []
    for key, group in table.groupby(
        ["market", "threshold", "crossed", "horizon"], sort=True
    ):
        weights = group["notional"].to_numpy()
        rows.append(
            {
                "market": key[0],
                "threshold": key[1],
                "crossed": key[2],
                "horizon": key[3],
                "n": len(group),
                "symbols": group["symbol"].nunique(),
                "hold_median": float(group["hold"].median()),
                "hold_mean": float(group["hold"].mean()),
                "actual_median": float(group["actual"].median()),
                "cut_better": float((group["cut_minus_actual"] > 0).mean()),
                "cut_minus_actual_wtd": float(
                    np.average(group["cut_minus_actual"], weights=weights)
                ),
                "cut_minus_actual_mean": float(group["cut_minus_actual"].mean()),
                "worst_after_median": float(group["worst_after"].median()),
                "sold_share_mean": float(group["sold_share"].mean()),
            }
        )
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
