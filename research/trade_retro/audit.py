"""Data checks that answer dots' cross-check, each with a count and a treatment.

Every count is computed from the export, so a later export reruns the same checks.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from research.trade_retro.canonical import LIVE_ALIASES, Canon
from research.trade_retro.export import Export

_TZ = {"kr": "Asia/Seoul", "us": "America/New_York", "crypto": "UTC"}
_ECONOMIC = [
    "account_alias",
    "market",
    "symbol",
    "side",
    "filled_qty",
    "fill_price",
    "filled_at_utc",
]


def _live_unflagged(export: Export) -> pd.DataFrame:
    frame = export.tables["execution_ledger"]
    return frame[
        frame["account_alias"].isin(LIVE_ALIASES)
        & (frame["superseded_duplicate"] == "false")
    ].copy()


def economic_duplicates(export: Export, canon: Canon) -> dict:
    """Rows equal on every economic field, and whether they overfill their order.

    The export has no ``fill_seq`` and its times can be cut to the minute, so separate
    trades of one order can look identical. An order whose rows sum to no more than its
    quantity holds real trades, and summing them is right.
    """
    rows = _live_unflagged(export)
    rows = rows[rows["fill_source"] != "manual_import"]
    repeated = rows[rows.duplicated(_ECONOMIC, keep=False)]
    groups = repeated.groupby(_ECONOMIC).ngroups if len(repeated) else 0
    extra = int(rows.duplicated(_ECONOMIC).sum())

    quantity = canon.orders.groupby("order_ref")["qty"].max()
    total = pd.to_numeric(rows["filled_qty"]).groupby(rows["order_ref"]).sum()
    within = over = no_order = 0
    for ref in repeated["order_ref"].unique():
        if ref not in quantity.index:
            no_order += 1
        elif total[ref] <= quantity[ref] * (1 + 1e-6):
            within += 1
        else:
            over += 1
    return {
        "groups": groups,
        "extra_rows": extra,
        "orders": int(repeated["order_ref"].nunique()),
        "orders_within_quantity": within,
        "orders_over_quantity": over,
        "orders_without_row": no_order,
    }


def quantity_mismatches(export: Export, canon: Canon) -> dict:
    """Orders where the ledger and the order row disagree, before and after cleaning."""
    rows = _live_unflagged(export)
    rows = rows[rows["fill_source"] != "manual_import"]
    raw_total = (
        pd.to_numeric(rows["filled_qty"])
        .groupby([rows["account_alias"], rows["order_ref"]])
        .sum()
    )
    orders = canon.orders
    before_over = 0
    for row in orders.itertuples(index=False):
        ledger = raw_total.get((row.alias, row.order_ref), 0.0)
        if row.alias != "kis_live" and row.qty > 0 and ledger > row.qty * (1 + 1e-6):
            before_over += 1
    # A broker that reuses order numbers needs the day in its key, so its rows are
    # counted from the keyed join: fills on the order's key over its quantity.
    keyed = canon.diagnostics["orders_over_quantity_before_cleaning_keyed"]
    return {
        "over_quantity_before": before_over + keyed,
        "over_quantity_after": int(
            (
                orders["ledger_qty"]
                > orders["qty"].where(orders["qty"] > 0) * (1 + 1e-6)
            ).sum()
        ),
        "row_says_less_than_ledger": int(
            (orders["row_filled"] < orders["ledger_qty"] - 1e-6).sum()
        ),
        "row_says_more_than_ledger": canon.diagnostics["order_ledger_only_fills"],
    }


def join_coverage(executions: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """How much of the ledger has an order row, and how much of the orders has a fill."""
    first_order = {
        alias: group["created"].min().date() for alias, group in orders.groupby("alias")
    }
    filled_orders = orders[orders["row_filled"] > 1e-9]
    rows = []
    for (alias, market), group in executions.groupby(["alias", "market"], sort=True):
        external = group["source"].isin(("external", "external_micro"))
        micro = group["source"] == "external_micro"
        # An account with fills and no order rows has nothing after a first order row.
        start = first_order.get(alias)
        after = group[group["day"] >= start] if start is not None else group.iloc[0:0]
        after_external = after["source"].isin(("external", "external_micro"))
        own = filled_orders[
            (filled_orders["alias"] == alias) & (filled_orders["market"] == market)
        ]
        rows.append(
            {
                "alias": alias,
                "market": market,
                "executed_orders": len(group),
                "with_order_row": int((~external).sum()),
                "with_order_row_share": float((~external).mean()),
                "notional_share": float(
                    group.loc[~external, "notional"].sum() / group["notional"].sum()
                ),
                # Each executed order counts in one column, micro-buys first.
                "before_first_order_row": int(
                    (group["day"] < start).where(~micro & external, False).sum()
                )
                if start is not None
                else int((~micro & external).sum()),
                "micro": int(micro.sum()),
                "other_after_first_order_row": int(
                    (after_external & (after["source"] == "external")).sum()
                ),
                "filled_order_rows": len(own),
                "filled_rows_with_ledger": int((own["ledger_qty"] > 1e-9).sum()),
            }
        )
    return pd.DataFrame(rows)


def external_refs_on_other_rows(executions: pd.DataFrame, orders: pd.DataFrame) -> int:
    """External executions whose hashed reference is on any order row of that account.

    A non-zero count would point at a broken join key. Zero means the two tables simply
    hold different populations.
    """
    known = set(zip(orders["alias"], orders["order_ref"], strict=True))
    external = executions[executions["source"].isin(("external", "external_micro"))]
    refs = [key.split("|")[1] for key in external["key"]]
    return sum((a, r) in known for a, r in zip(external["alias"], refs, strict=True))


def non_session_stamps(export: Export) -> pd.DataFrame:
    """Rows stamped on a weekend, local to the market. Crypto trades every day."""
    rows = _live_unflagged(export)
    rows = rows[rows["market"].isin(("kr", "us", "crypto"))]
    stamps = pd.to_datetime(rows["filled_at_utc"], format="ISO8601")
    weekday = [
        stamp.astimezone(ZoneInfo(_TZ[market])).weekday()
        for stamp, market in zip(stamps, rows["market"], strict=True)
    ]
    rows = rows.assign(weekend=[d >= 5 for d in weekday])
    out = []
    for (market, source), group in rows.groupby(["market", "fill_source"], sort=True):
        out.append(
            {
                "market": market,
                "source": source,
                "rows": len(group),
                "weekend": int(group["weekend"].sum()),
            }
        )
    return pd.DataFrame(out)


def order_waterfall(orders: pd.DataFrame) -> pd.DataFrame:
    """From every order row to the accepted orders the fill rates use."""
    rows = []
    for market, group in orders.groupby("market", sort=True):
        cancel = group["operation"] == "cancel"
        rejected = ~cancel & (group["outcome"] == "rejected")
        unaccepted = ~cancel & ~rejected & ~group["accepted"]
        parking = ~cancel & ~rejected & ~unaccepted & group["parking"]
        rows.append(
            {
                "market": market,
                "rows": len(group),
                "cancel_rows": int(cancel.sum()),
                "rejected": int(rejected.sum()),
                "never_accepted": int(unaccepted.sum()),
                "parking": int(parking.sum()),
                "accepted": int((group["is_accepted"] & ~group["parking"]).sum()),
            }
        )
    return pd.DataFrame(rows)
