"""Question 3: deep limits against buying near the market, per intent.

The retrospective already measured fill rates and the next-open counterfactual (R1).
This adds what the whole intent stream earned under each style, with an unfilled
intent counted as cash at 0%:

* An intent is one account, symbol, side and local ISO week (the retro's key). Its
  style is that of its first accepted order: ``deep_limit`` (limit at least 3% below
  the last complete close) or ``near_market``.
* ``actual``: a filled intent earns from its volume-weighted fill price to the close
  ``h`` sessions after its first fill session. An unfilled one earns 0%.
* ``next open``: every intent is bought at the first open after its first order and
  earns to the close ``h`` sessions after that open. For an unfilled intent this is the
  opportunity cost of not filling. Fees are left out of both, so they compare like
  for like (one buy leg each); costs are reported separately.
* ``fill vs next open``: for a filled intent, how much cheaper its fill was than the
  next open. Positive means the patient limit bought lower.
"""

from __future__ import annotations

import pandas as pd

from research.strategy_evidence.averaging import order_fills
from research.trade_retro.canonical import next_open_session
from research.trade_retro.prices import PriceBook

HORIZONS = (5, 20)
INTENT_COLUMNS = [
    "intent", "alias", "market", "symbol", "klass", "source", "entry_type", "distance", "filled",
    "open_day", "open_price", "qty", "fill_vs_next_open",
    "next_open_5", "next_open_x5", "actual_5", "next_open_20", "next_open_x20", "actual_20",
]  # fmt: skip
STYLE_COLUMNS = [
    "market", "klass", "intents", "filled", "fill_rate", "fill_vs_next_open",
    *(
        f"{name.format(h=h)}"
        for h in HORIZONS
        for name in ("n{h}", "actual_{h}", "next_open_{h}", "next_open_x{h}", "unfilled_n{h}",
                     "unfilled_cost_{h}", "unfilled_cost_median_{h}")
    ),
]  # fmt: skip


def intent_table(
    orders: pd.DataFrame, fills: pd.DataFrame, book: PriceBook
) -> pd.DataFrame:
    """One row per buy intent with its actual and next-open outcomes."""
    pool = orders[
        orders["is_accepted"] & (orders["side"] == "buy") & ~orders["parking"]
    ].sort_values("created", kind="stable")
    per_key = order_fills(fills)
    rows = []
    for intent, group in pool.groupby("intent", sort=True):
        first = group.iloc[0]
        bars = book.get(first["market"], first["symbol"])
        if bars is None or first["klass"] == "no_reference":
            continue
        filled_keys = [k for k in group["key"] if k in per_key.index]
        record = {
            "intent": intent,
            "alias": first["alias"],
            "market": first["market"],
            "symbol": first["symbol"],
            "klass": first["klass"],
            "source": first["source"],
            "entry_type": first["entry_type"],
            "distance": first["distance"],
            "filled": bool(filled_keys),
        }
        i = next_open_session(bars, first["market"], first["created"])
        open_price = float(bars.open[i]) if i is not None else None
        if i is not None:
            record["open_day"] = bars.date_at(i)
            record["open_price"] = open_price
            record["qty"] = float(first["qty"]) * bars.split_factor_after(
                bars.date_at(i)
            )
        fill_price = fill_i = None
        if filled_keys:
            sub = per_key.loc[filled_keys]
            fill_price = float(sub["notional"].sum() / sub["qty"].sum())
            fill_i = bars.idx_on_or_after(sub["first_day"].min())
            record["fill_vs_next_open"] = (
                open_price / fill_price - 1.0 if open_price else None
            )
        for h in HORIZONS:
            cf = None
            if i is not None and i + h < len(bars):
                cf = float(bars.close[i + h]) / open_price - 1.0
                index = book.index_return_from_open(
                    first["market"], bars.date_at(i), bars.date_at(i + h)
                )
                record[f"next_open_x{h}"] = None if index is None else cf - index
            record[f"next_open_{h}"] = cf
            if filled_keys:
                actual = (
                    float(bars.close[fill_i + h]) / fill_price - 1.0
                    if fill_i is not None and fill_i + h < len(bars)
                    else None
                )
            else:
                actual = 0.0 if cf is not None else None
            record[f"actual_{h}"] = actual
        rows.append(record)
    return pd.DataFrame(rows, columns=INTENT_COLUMNS)


def summarize_styles(intents: pd.DataFrame) -> pd.DataFrame:
    """Per market and style. Means are over intents, equal weight."""
    rows = []
    for (market, klass), group in intents.groupby(["market", "klass"], sort=True):
        record = {
            "market": market,
            "klass": klass,
            "intents": len(group),
            "filled": int(group["filled"].sum()),
            "fill_rate": float(group["filled"].mean()),
            "fill_vs_next_open": float(
                group.loc[group["filled"], "fill_vs_next_open"].mean()
            )
            if group["filled"].any()
            else float("nan"),
        }
        for h in HORIZONS:
            both = group[group[f"actual_{h}"].notna() & group[f"next_open_{h}"].notna()]
            unfilled = group[~group["filled"] & group[f"next_open_{h}"].notna()]
            record[f"n{h}"] = len(both)
            record[f"actual_{h}"] = float(both[f"actual_{h}"].mean())
            record[f"next_open_{h}"] = float(both[f"next_open_{h}"].mean())
            record[f"next_open_x{h}"] = float(both[f"next_open_x{h}"].mean())
            record[f"unfilled_n{h}"] = len(unfilled)
            record[f"unfilled_cost_{h}"] = float(unfilled[f"next_open_{h}"].mean())
            record[f"unfilled_cost_median_{h}"] = float(
                unfilled[f"next_open_{h}"].median()
            )
        rows.append(record)
    return pd.DataFrame(rows, columns=STYLE_COLUMNS)


def near_market_overlay(intents: pd.DataFrame) -> pd.DataFrame:
    """Every unfilled intent bought at the next open with its first order's quantity
    and held to the last bar, as overlay trades."""
    pool = intents[~intents["filled"] & intents["open_price"].notna()]
    return pd.DataFrame(
        {
            "alias": pool["alias"],
            "market": pool["market"],
            "symbol": pool["symbol"],
            "day": pool["open_day"],
            "qty": pool["qty"],
            "price": pool["open_price"],
        }
    ).reset_index(drop=True)
