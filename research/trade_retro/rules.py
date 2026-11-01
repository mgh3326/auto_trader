"""Candidate rules and their counterfactuals, fixed in advance for every market.

The list is the same for all three markets and every result is reported, whichever
way it points, so the set is not picked for its outcome. All of it is in-sample on
one six-month window. None of it models price impact, fees or available cash.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.trade_retro.canonical import is_parking, next_open_session
from research.trade_retro.prices import PriceBook

EXIT_HORIZON = 20


def _in_window_lots(matches: pd.DataFrame) -> pd.DataFrame:
    """Lots bought in the window, without the cash-like parking instruments."""
    parking = np.array(
        [
            is_parking(m, s)
            for m, s in zip(matches["market"], matches["symbol"], strict=True)
        ],
        dtype=bool,
    )
    return matches[(matches["origin"] == "fill").to_numpy() & ~parking]


def unfilled_buy_counterfactual(orders: pd.DataFrame, book: PriceBook) -> pd.DataFrame:
    """R1: accepted buys that never filled, bought at the next session's open instead.

    One order per intent (account, symbol, side, local ISO week), so a resubmitted
    order counts once, and an intent where any order filled is left out. The return
    runs from the first open after the order to the close 5 and 20 sessions on, and
    the index leg starts at that same open.
    """
    filled_intents = set(orders.loc[orders["filled_qty"] > 1e-9, "intent"])
    pool = orders[
        orders["is_accepted"]
        & (orders["side"] == "buy")
        & (orders["filled_qty"] <= 1e-9)
        & ~orders["parking"]
        & ~orders["intent"].isin(filled_intents)
    ].sort_values("created", kind="stable")
    pool = pool.drop_duplicates("intent")
    rows = []
    for row in pool.itertuples(index=False):
        bars = book.get(row.market, row.symbol)
        if bars is None:
            continue
        i = next_open_session(bars, row.market, row.created)
        if i is None:
            continue
        price = float(bars.open[i])
        qty = row.qty * bars.split_factor_after(bars.date_at(i))
        record = {"market": row.market, "klass": row.klass, "symbol": row.symbol}
        record["notional"] = qty * price
        for horizon in (5, 20):
            j = i + horizon
            if j < len(bars):
                ret = float(bars.close[j]) / price - 1.0
                index_ret = book.index_return_from_open(
                    row.market, bars.date_at(i), bars.date_at(j)
                )
                record[f"r{horizon}"] = ret
                record[f"x{horizon}"] = None if index_ret is None else ret - index_ret
            else:
                record[f"r{horizon}"] = None
                record[f"x{horizon}"] = None
        rows.append(record)
    return pd.DataFrame(rows)


def sell_family(row) -> str:
    """Group a sell by what the export records about why it happened."""
    if row["source"] in ("external", "external_micro"):
        return "no_order_row"
    if row["exit_intent"] == "loss_cut":
        return "loss_cut"
    tags = f"{row['tag']} {row['proposal_tag']}"
    if "trim_preplace" in tags:
        return "trim_preplace"
    if row["source"] == "proposal":
        return "other_proposal"
    return "session"


def hold_instead_of_sell(executions: pd.DataFrame) -> pd.DataFrame:
    """R2: the sell never happens and the shares are held on.

    ``hold_gain`` at a horizon is what holding would have added over selling, as a
    fraction of the sold notional. A positive value means the sale cost money.
    """
    sells = executions[(executions["side"] == "sell") & ~executions["parking"]].copy()
    sells["family"] = sells.apply(sell_family, axis=1)
    return sells


def time_exit_counterfactual(
    matches: pd.DataFrame, book: PriceBook, horizon: int = EXIT_HORIZON
) -> pd.DataFrame:
    """R3: every in-window lot is sold at the close ``horizon`` sessions after entry.

    A lot too young to reach the horizon keeps its mark at the last bar.
    """
    rows = []
    lots = _in_window_lots(matches)
    for row in lots.itertuples(index=False):
        bars = book.get(row.market, row.symbol)
        if bars is None:
            continue
        i = bars.idx_on_or_after(row.entry_day)
        if i is None:
            continue
        j = min(i + horizon, len(bars) - 1)
        entry_price = row.cost / row.qty
        rule_pnl = row.qty * (float(bars.close[j]) - entry_price)
        rows.append(
            {
                "market": row.market,
                "lot": row.lot,
                "cost": row.cost,
                "actual_pnl": row.pnl,
                "rule_pnl": rule_pnl,
                "reached": i + horizon < len(bars),
            }
        )
    return pd.DataFrame(rows)


def lot_entry_types(matches: pd.DataFrame, executions: pd.DataFrame) -> pd.DataFrame:
    """R4 and R5: in-window lots with the entry type of the order that opened them."""
    types = executions.drop_duplicates("key").set_index("key")["entry_type"]
    lots = _in_window_lots(matches).copy()
    lots["entry_type"] = lots["entry_key"].map(types).fillna("unknown")
    return lots


def weighted(values: pd.Series, weights: pd.Series) -> float:
    mask = values.notna()
    if not mask.any():
        return float("nan")
    return float(
        np.average(values[mask].astype(float), weights=weights[mask].astype(float))
    )
