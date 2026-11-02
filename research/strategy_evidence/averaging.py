"""Question 1: averaging down, scored under each tier's own frozen D+20 rule.

``underwater-d20-v1`` (``buy.underwater_support_net``, KR and US): D0 is an order's
last actual fill date, D+20 is the close twenty sessions later on the symbol's own
bars, and the observation is that close against the order's execution price. Fills
before 2026-09-08 are outside the contract. Unmatured observations are censored, and
with fewer than five mature ones the result is INSUFFICIENT_SAMPLE.

The export keeps the tag ``underwater_support_net`` in clear text, so the tier's own
orders are identified exactly. ``held_majors_support_net`` never appears in the
export (its orders carry hashed tags), so the crypto tier is measured through a
proxy defined below, and labelled as one.

Because the tagged cohort is tiny, the same D+20 statistic is also computed for every
real buy into an already held position, bucketed by that position's unrealized P&L
just before the buy. That is averaging down as it was actually done, whatever the tag.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from research.strategy_evidence.common import TOLERANCE
from research.strategy_evidence.lots import state_before
from research.trade_retro.canonical import reference_close
from research.trade_retro.prices import PriceBook

TIER_TAG = "underwater_support_net"
CONTRACT_START = date(2026, 9, 8)
HELD_MAJORS_ARMED = date(2026, 8, 22)
D20 = 20
LOSS_FLOOR = -0.08
SUPPORT_BAND = (3.0, 12.0)
TIER_COLUMNS = [
    "market", "alias", "symbol", "created", "accepted", "outcome", "distance", "filled",
    "lot_known", "lot_unrealized", "d0", "exec_price", "in_contract", "d20", "d20_index",
]  # fmt: skip
BUY_COLUMNS = [
    "market", "alias", "symbol", "key", "klass", "distance", "micro", "d0", "notional", "bucket",
    "unrealized_at_fill", "unrealized_at_decision", "d5", "d20", "d20_index",
]  # fmt: skip


def order_fills(fills: pd.DataFrame) -> pd.DataFrame:
    """One row per order key: quantity, execution price, first and last fill."""
    real = fills[~fills["seed"] & fills["key"].notna()].copy()
    real["notional"] = real["qty"] * real["price"]
    out = real.groupby("key", sort=True).agg(
        qty=("qty", "sum"),
        notional=("notional", "sum"),
        first_day=("date", "min"),
        last_day=("date", "max"),
        first_ts=("ts", "min"),
    )
    out["price"] = out["notional"] / out["qty"]
    return out


def forward_return(
    book: PriceBook, market: str, symbol: str, day: date, price: float, horizon: int
) -> tuple[float | None, float | None]:
    """Close ``horizon`` sessions after ``day`` against ``price``, and the same span's
    index return. ``None`` when the horizon is past the last bar (censored)."""
    bars = book.get(market, symbol)
    if bars is None:
        return None, None
    i = bars.idx_on_or_after(day)
    if i is None or i + horizon >= len(bars):
        return None, None
    ret = float(bars.close[i + horizon]) / price - 1.0
    index = book.index_return(market, bars.date_at(i), bars.date_at(i + horizon))
    return ret, index


def _lot_state(
    fills: pd.DataFrame, cutover: datetime, row, stamp, key
) -> tuple[bool, float, float]:
    rows = fills[
        (fills["alias"] == row.alias)
        & (fills["market"] == row.market)
        & (fills["symbol"] == row.symbol)
    ]
    state = state_before(rows, cutover, stamp, key)
    return state.known, state.qty, state.avg


def tier_orders(
    orders: pd.DataFrame, fills: pd.DataFrame, book: PriceBook, cutover: datetime
) -> pd.DataFrame:
    """Every order tagged ``underwater_support_net``, with its D+20 observation."""
    tagged = orders[
        ((orders["tag"] == TIER_TAG) | (orders["proposal_tag"] == TIER_TAG))
        & (orders["side"] == "buy")
    ]
    per_key = order_fills(fills)
    rows = []
    for row in tagged.itertuples(index=False):
        record = {
            "market": row.market,
            "alias": row.alias,
            "symbol": row.symbol,
            "created": row.created,
            "accepted": bool(row.is_accepted),
            "outcome": row.outcome,
            "distance": row.distance,
            "filled": row.key in per_key.index,
        }
        known, qty, avg = _lot_state(fills, cutover, row, row.created, None)
        bars = book.get(row.market, row.symbol)
        ref = reference_close(bars, row.market, row.created) if bars else None
        record["lot_known"] = known
        record["lot_unrealized"] = ref / avg - 1.0 if known and ref else None
        if record["filled"]:
            fill = per_key.loc[row.key]
            record["d0"] = fill["last_day"]
            record["exec_price"] = float(fill["price"])
            record["in_contract"] = fill["last_day"] >= CONTRACT_START
            ret, index = forward_return(
                book, row.market, row.symbol, fill["last_day"], fill["price"], D20
            )
            record["d20"] = ret
            record["d20_index"] = index
        rows.append(record)
    return pd.DataFrame(rows, columns=TIER_COLUMNS)


def add_bucket(known: bool, qty: float, unrealized: float | None) -> str:
    if qty <= 1e-9:
        return "new entry (flat before)"
    if not known or unrealized is None:
        return "add, cost unknown"
    if unrealized <= LOSS_FLOOR + TOLERANCE:
        return "add, lot <= -8%"
    if unrealized < 0:
        return "add, lot -8% to 0%"
    return "add, lot >= 0%"


def buys_by_lot_state(
    executions: pd.DataFrame,
    orders: pd.DataFrame,
    fills: pd.DataFrame,
    book: PriceBook,
    cutover: datetime,
) -> pd.DataFrame:
    """Every real buy (not parking), with the position's state just before it and the
    D+20 observation under the same contract arithmetic."""
    per_key = order_fills(fills)
    buys = executions[(executions["side"] == "buy") & ~executions["parking"]]
    info = orders.drop_duplicates("key").set_index("key")
    rows = []
    for row in buys.itertuples(index=False):
        fill = per_key.loc[row.key]
        known, qty, avg = _lot_state(fills, cutover, row, fill["first_ts"], row.key)
        unrealized = float(fill["price"]) / avg - 1.0 if known else None
        order = info.loc[row.key] if row.key in info.index else None
        ref_unrealized = None
        if order is not None and known:
            bars = book.get(row.market, row.symbol)
            ref = reference_close(bars, row.market, order["created"]) if bars else None
            ref_unrealized = ref / avg - 1.0 if ref else None
        ret5, index5 = forward_return(
            book, row.market, row.symbol, fill["last_day"], fill["price"], 5
        )
        ret20, index20 = forward_return(
            book, row.market, row.symbol, fill["last_day"], fill["price"], D20
        )
        rows.append(
            {
                "market": row.market,
                "alias": row.alias,
                "symbol": row.symbol,
                "key": row.key,
                "klass": row.klass,
                "distance": None if order is None else order["distance"],
                "micro": row.klass == "external_micro",
                "d0": fill["last_day"],
                "notional": float(fill["notional"]),
                "bucket": add_bucket(known, qty, unrealized),
                "unrealized_at_fill": unrealized,
                "unrealized_at_decision": ref_unrealized,
                "d5": ret5,
                "d20": ret20,
                "d20_index": index20,
            }
        )
    return pd.DataFrame(rows, columns=BUY_COLUMNS)


def held_majors_proxy(buys: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Crypto buys that look like ``held_majors_support_net`` placements.

    Strict: an order row, filled on or after the tier was armed, into a held coin in
    profit at the decision (last complete close against average cost), with a limit
    3-12% below that close. Loose: drops the distance clause. Both exclude recurring
    micro-buys. Neither proves the session meant the tier.
    """
    crypto = buys[
        (buys["market"] == "crypto")
        & ~buys["micro"]
        & (buys["klass"] != "external")
        & (buys["d0"] >= HELD_MAJORS_ARMED)
    ]
    profitable = crypto[crypto["unrealized_at_decision"].fillna(-1.0) > 0]
    low, high = SUPPORT_BAND
    strict = profitable[
        profitable["distance"].notna()
        & (profitable["distance"] >= low)
        & (profitable["distance"] <= high)
    ]
    return {"strict": strict, "loose": profitable}


def average_down_overlay(
    paths: pd.DataFrame, threshold: float, fraction: float = 0.5
) -> pd.DataFrame:
    """Systematic averaging down on the real book: at each episode's first close at or
    below -``threshold`` against average cost, buy ``fraction`` of the quantity then
    held at that close, once per episode, and hold it to the last bar."""
    from research.strategy_evidence.losscut import first_touches

    rows = [
        {
            "alias": t.alias,
            "market": t.market,
            "symbol": t.symbol,
            "day": t.day,
            "qty": fraction * t.qty,
            "price": t.price,
        }
        for t in first_touches(paths, threshold)
    ]
    return pd.DataFrame(
        rows, columns=["alias", "market", "symbol", "day", "qty", "price"]
    )
