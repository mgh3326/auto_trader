"""Question 5: crypto sell orders resting above the market.

Each accepted crypto sell is placed by its distance above the last complete UTC
daily close (the retro's reference). A resting take-profit is one at least 1% above.
For each order, against the next daily open after it was placed (O):

* ``rest``: what the order's coins became by the last bar. A filled part sold at its
  fill price and then sat in cash at 0%; an unfilled part is marked at the last close.
* ``sell now``: the whole quantity sold at O, then cash.
* ``hold``: never sold, marked at the last close.

``rest vs sell now`` is (rest / O - 1): positive means waiting paid. ``rest vs hold``
is the same against the hold value. Days to fill are counted in UTC days between the
order's creation and its first fill row; a reconciler row can carry the order's own
time, so a same-day fill may be earlier than it really was, never later.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.strategy_evidence.averaging import order_fills
from research.trade_retro.canonical import next_open_session, session_date
from research.trade_retro.prices import PriceBook

TAKE_PROFIT_MIN = 1.0
COLUMNS = [
    "symbol", "alias", "created_day", "distance", "band", "take_profit", "outcome", "qty",
    "filled_share", "days_to_fill", "age_at_end", "fill_day", "fill_price", "fill_qty",
    "open_next", "open_day", "rest_vs_sell_now", "hold_vs_sell_now", "rest_vs_hold",
    "btc_same_span", "notional",
]  # fmt: skip
BANDS = ((-np.inf, 1.0, "a. below +1%"), (1.0, 3.0, "b. +1 to +3%"),
         (3.0, 8.0, "c. +3 to +8%"), (8.0, 16.0, "d. +8 to +16%"),
         (16.0, np.inf, "e. +16% or more"))  # fmt: skip


def band(distance: float) -> str:
    for low, high, name in BANDS:
        if low <= distance < high:
            return name
    return "no_reference"


def sell_orders(
    orders: pd.DataFrame, fills: pd.DataFrame, book: PriceBook
) -> pd.DataFrame:
    pool = orders[
        orders["is_accepted"]
        & (orders["market"] == "crypto")
        & (orders["side"] == "sell")
    ]
    per_key = order_fills(fills)
    rows = []
    for row in pool.itertuples(index=False):
        bars = book.get(row.market, row.symbol)
        if bars is None or row.distance != row.distance:
            continue
        created_day = session_date("crypto", row.created)
        i = next_open_session(bars, "crypto", row.created)
        last = len(bars) - 1
        close_end = float(bars.close[last])
        record = {
            "symbol": row.symbol,
            "alias": row.alias,
            "created_day": created_day,
            "distance": float(row.distance),
            "band": band(float(row.distance)),
            "take_profit": row.distance >= TAKE_PROFIT_MIN,
            "outcome": row.outcome,
            "qty": float(row.qty),
            "filled_share": 0.0,
            "days_to_fill": None,
            "age_at_end": (bars.date_at(last) - created_day).days,
        }
        if row.key in per_key.index:
            fill = per_key.loc[row.key]
            share = min(float(fill["qty"]) / float(row.qty), 1.0) if row.qty else 1.0
            record["filled_share"] = share
            record["days_to_fill"] = max((fill["first_day"] - created_day).days, 0)
            fill_price = float(fill["price"])
            record["fill_day"] = fill["first_day"]
            record["fill_price"] = fill_price
            record["fill_qty"] = share * float(row.qty)
        else:
            share, fill_price = 0.0, 0.0
        if i is not None:
            open_price = float(bars.open[i])
            rest = share * fill_price + (1.0 - share) * close_end
            record["open_next"] = open_price
            record["open_day"] = bars.date_at(i)
            record["rest_vs_sell_now"] = rest / open_price - 1.0
            record["hold_vs_sell_now"] = close_end / open_price - 1.0
            record["rest_vs_hold"] = rest / close_end - 1.0
            record["btc_same_span"] = book.index_return_from_open(
                "crypto", bars.date_at(i), bars.date_at(last)
            )
            record["notional"] = float(row.qty) * open_price
        rows.append(record)
    return pd.DataFrame(rows, columns=COLUMNS)


def summarize_ladders(table: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, group in table.groupby("band", sort=True):
        filled = group[group["filled_share"] > 0]
        still_open = group[group["outcome"] == "open"]
        priced = group[group["rest_vs_sell_now"].notna()]
        weights = priced["notional"].fillna(0.0)
        rows.append(
            {
                "band": name,
                "orders": len(group),
                "filled": len(filled),
                "fill_rate": len(filled) / len(group),
                "days_to_fill_median": float(filled["days_to_fill"].median())
                if len(filled)
                else float("nan"),
                "days_to_fill_max": float(filled["days_to_fill"].max())
                if len(filled)
                else float("nan"),
                "open_at_end": len(still_open),
                "open_age_median": float(still_open["age_at_end"].median())
                if len(still_open)
                else float("nan"),
                "rest_vs_sell_now": float(group["rest_vs_sell_now"].mean()),
                "rest_vs_sell_now_wtd": float(
                    np.average(priced["rest_vs_sell_now"], weights=weights)
                )
                if weights.sum() > 0
                else float("nan"),
                "rest_vs_hold": float(group["rest_vs_hold"].mean()),
                "hold_vs_sell_now": float(group["hold_vs_sell_now"].mean()),
                "btc_same_span": float(group["btc_same_span"].mean()),
            }
        )
    return pd.DataFrame(rows)


def sell_now_overlay(table: pd.DataFrame) -> pd.DataFrame:
    """Each resting take-profit sold at the next open instead, as overlay trades:
    sell the order's quantity at that open and undo its real fill."""
    rows = []
    pool = table[table["take_profit"] & table["open_next"].notna()]
    for row in pool.itertuples(index=False):
        base = {"alias": row.alias, "market": "crypto", "symbol": row.symbol}
        rows.append(
            {**base, "day": row.open_day, "qty": -row.qty, "price": row.open_next}
        )
        if row.filled_share > 0:
            rows.append(
                {
                    **base,
                    "day": row.fill_day,
                    "qty": row.fill_qty,
                    "price": row.fill_price,
                }
            )
    return pd.DataFrame(
        rows, columns=["alias", "market", "symbol", "day", "qty", "price"]
    )
