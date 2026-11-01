"""Entry-type classification, fill rates and the executions behind the markouts.

The export has no quote at order time, so the reference price is the last daily
close complete when the order was created. That differs from the live price the
policy band uses by any overnight or intraday move.
"""

from __future__ import annotations

import pandas as pd

from research.trade_retro.canonical import (
    is_parking,
    reference_close,
    session_date,
)
from research.trade_retro.markouts import HORIZONS, index_excess, markout
from research.trade_retro.prices import PriceBook

#: ``buy.deep_limit_pct_range`` in config/trading_policy.yaml is [-12, -3].
DEEP_PCT = 3.0
#: A fill with no order row at or below this size is a recurring micro-buy.
MICRO_NOTIONAL_KRW = 6000.0


def distance_pct(side: str, limit: float, reference: float) -> float:
    """How far the limit sits from the reference, positive when it is away from the
    market: below it for a buy, above it for a sell."""
    pct = (limit / reference - 1.0) * 100.0
    return -pct if side == "buy" else pct


def distance_class(order_type: str, distance: float | None) -> str:
    if order_type == "market":
        return "near_market"
    if distance is None:
        return "no_reference"
    return "deep_limit" if distance >= DEEP_PCT else "near_market"


def distance_band(distance: float | None) -> str:
    if distance is None:
        return "no_reference"
    if distance >= 12.0:
        return "a. >=12%"
    if distance >= 3.0:
        return "b. 3-12%"
    if distance >= 1.0:
        return "c. 1-3%"
    if distance >= 0.0:
        return "d. 0-1%"
    return "e. marketable"


def entry_type(klass: str, source: str) -> str:
    return f"{klass}/{source}"


def classify_orders(orders: pd.DataFrame, book: PriceBook) -> pd.DataFrame:
    """Add distance, class, entry type and the accepted-order flag."""
    orders = orders.copy()
    distances: list[float | None] = []
    for row in orders.itertuples(index=False):
        bars = book.get(row.market, row.symbol)
        reference = (
            reference_close(bars, row.market, row.created) if bars is not None else None
        )
        usable = reference and row.limit_price == row.limit_price
        # The close is in split-adjusted units and the limit is as sent.
        distances.append(
            distance_pct(
                row.side,
                row.limit_price
                / bars.split_factor_after(session_date(row.market, row.created)),
                reference,
            )
            if usable
            else None
        )
    orders["distance"] = distances
    orders["klass"] = [
        distance_class(t, d)
        for t, d in zip(orders["order_type"], orders["distance"], strict=True)
    ]
    orders["band"] = [distance_band(d) for d in orders["distance"]]
    orders["entry_type"] = [
        entry_type(k, s) for k, s in zip(orders["klass"], orders["source"], strict=True)
    ]
    orders["parking"] = [
        is_parking(m, s)
        for m, s in zip(orders["market"], orders["symbol"], strict=True)
    ]
    # An accepted order reached the broker, and a cancel row is not an order.
    orders["is_accepted"] = (
        orders["accepted"]
        & (orders["outcome"] != "rejected")
        & (orders["operation"] != "cancel")
    )
    orders["intent"] = [
        "{}|{}|{}|{}-{}".format(a, sym, side, *session_date(m, c).isocalendar()[:2])
        for a, m, sym, side, c in zip(
            orders["alias"],
            orders["market"],
            orders["symbol"],
            orders["side"],
            orders["created"],
            strict=True,
        )
    ]
    return orders


def fill_rates(orders: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Fill rate of accepted orders. ``open`` counts as unfilled in ``fill_rate`` and
    is removed from the denominator in ``fill_rate_closed``, because ``open`` means
    no ending was recorded, not that the order is live."""
    accepted = orders[orders["is_accepted"]]
    rows = []
    for key, group in accepted.groupby(by, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        filled = group["filled_qty"] > 1e-9
        known = group["outcome"] != "open"
        intents = filled.groupby(group["intent"]).any()
        rows.append(
            dict(
                zip(by, key, strict=True),
                accepted=len(group),
                filled=int(filled.sum()),
                partial=int((group["outcome"] == "partial").sum()),
                expired=int((group["outcome"] == "expired").sum()),
                cancelled=int((group["outcome"] == "cancelled").sum()),
                open=int((group["outcome"] == "open").sum()),
                fill_rate=float(filled.mean()),
                fill_rate_closed=(
                    float(filled[known].mean()) if known.any() else float("nan")
                ),
                intents=len(intents),
                intent_fill_rate=float(intents.mean()),
            )
        )
    return pd.DataFrame(rows)


def build_executions(
    fills: pd.DataFrame, orders: pd.DataFrame, book: PriceBook
) -> pd.DataFrame:
    """One row per executed order, with markouts. Partial fills are volume-weighted."""
    real = fills[~fills["seed"]].copy()
    real["notional"] = real["qty"] * real["price"]
    grouped = (
        real.groupby(["key", "alias", "market", "symbol", "side"], sort=True)
        .agg(qty=("qty", "sum"), notional=("notional", "sum"), day=("date", "min"))
        .reset_index()
    )
    grouped["price"] = grouped["notional"] / grouped["qty"]
    info = orders.drop_duplicates("key").set_index("key")
    rows = []
    for row in grouped.itertuples(index=False):
        record = row._asdict()
        if row.key in info.index:
            order = info.loc[row.key]
            record.update(
                entry_type=order["entry_type"],
                klass=order["klass"],
                source=order["source"],
                band=order["band"],
                tag=order["tag"],
                proposal_tag=order["proposal_tag"],
                rung_index=order["rung_index"],
                exit_intent=order["exit_intent"],
            )
        else:
            micro = row.market == "crypto" and row.notional <= MICRO_NOTIONAL_KRW
            klass = "external_micro" if micro else "external"
            record.update(
                entry_type=klass,
                klass=klass,
                source=klass,
                band="no_order_row",
                tag="",
                proposal_tag="",
                rung_index=None,
                exit_intent="",
            )
        record["parking"] = is_parking(row.market, row.symbol)
        bars = book.get(row.market, row.symbol)
        for horizon in HORIZONS:
            value = (
                markout(bars, row.day, row.price, row.side, horizon)
                if bars is not None
                else None
            )
            record[f"m{horizon}"] = value
            record[f"x{horizon}"] = (
                index_excess(value, book, row.market, bars, row.day, row.side, horizon)
                if bars is not None
                else None
            )
        rows.append(record)
    return pd.DataFrame(rows)
