"""Turn the export's tables into one order table and one fill table.

The rules here decide every later number, and each exists because of a measured
property of the export:

* Only real money: the KIS, Toss and Upbit live tables. Mock and paper are out.
* ``execution_ledger`` rows count when ``superseded_duplicate`` is false.
* Two kinds of ledger row repeat a fill and are removed first: a websocket row with
  an identical reconciler row on the next day, stamped a day early so the date-keyed
  supersede check misses it, and a running total reported as if it were one more
  partial fill of an order.
* A fill is the union of two sources. Order rows can be stale in both directions:
  a row can say ``open`` while the ledger holds its fills, and a Toss or KIS row
  can say ``filled`` with no ledger row at all. The larger quantity wins and a
  residual becomes a fill with source ``order_ledger_only``.
* KIS reuses order numbers across days, so its key includes ``trading_day``, with
  a one-day fallback for a fill whose recorded day differs from the order's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.symbol import to_db_symbol
from research.trade_retro.export import Export
from research.trade_retro.prices import Bars, PriceBook

LIVE_ORDER_TABLES = (
    "kis_live_order_ledger",
    "live_order_ledger",
    "toss_live_order_ledger",
)
LIVE_ALIASES = ("kis_live", "toss_live", "upbit_live")
#: The closed parking tuple from the task 817 contract: cash-like instruments.
PARKING = {("us", "SGOV"), ("us", "BIL"), ("kr", "459580"), ("kr", "357870")}
#: KRX daily limit. No fill in the export sits between 17% and 40% from its close,
#: so this separates real fills from rows that cannot be trades.
IMPLAUSIBLE_DEVIATION = 0.30

_TZ = {
    "kr": ZoneInfo("Asia/Seoul"),
    "us": ZoneInfo("America/New_York"),
    "crypto": ZoneInfo("UTC"),
}
_CLOSE = {"kr": time(15, 30), "us": time(16, 0)}
_OPEN = {"kr": time(9, 0), "us": time(9, 30)}
#: How far apart a websocket row and its identical reconciler row can be stamped.
TWIN_MAX_GAP = timedelta(days=2)


def normalize_symbol(market: str, symbol: str) -> str:
    text = symbol.strip().upper()
    if market == "crypto":
        return text.removeprefix("KRW-")
    return to_db_symbol(text)


def is_parking(market: str, symbol: str) -> bool:
    return (market, symbol) in PARKING


def parse_ts(value: str) -> datetime | None:
    return pd.Timestamp(value).to_pydatetime() if value else None


def session_date(market: str, stamp: datetime) -> date:
    """The market-local date. Crypto uses the UTC date, as Upbit's daily bar does."""
    return stamp.astimezone(_TZ[market]).date()


def after_close(market: str, stamp: datetime) -> bool:
    """Whether that day's bar was complete at ``stamp``. A crypto bar never is."""
    close = _CLOSE.get(market)
    return close is not None and stamp.astimezone(_TZ[market]).time() >= close


def reference_index(bars: Bars, market: str, stamp: datetime) -> int | None:
    """The last bar complete at ``stamp``: no look-ahead."""
    day = session_date(market, stamp)
    if not after_close(market, stamp):
        day -= timedelta(days=1)
    return bars.idx_on_or_before(day)


def reference_close(bars: Bars, market: str, stamp: datetime) -> float | None:
    i = reference_index(bars, market, stamp)
    return None if i is None else float(bars.close[i])


def next_open_session(bars: Bars, market: str, stamp: datetime) -> int | None:
    """The first session whose open comes after ``stamp``."""
    day = session_date(market, stamp)
    market_open = _OPEN.get(market)
    if market_open is None or stamp.astimezone(_TZ[market]).time() >= market_open:
        day += timedelta(days=1)
    return bars.idx_on_or_after(day)


@dataclass(frozen=True)
class Cleaning:
    """Which fill rules apply. All on is the report; the rest are sensitivity lines."""

    twins: bool = True
    totals: bool = True
    proven_partials: bool = True
    order_only: bool = True


@dataclass
class Canon:
    orders: pd.DataFrame
    fills: pd.DataFrame
    diagnostics: dict = field(default_factory=dict)


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _key(alias: str, ref: str, day: str) -> str:
    return f"{alias}|{ref}|{day}" if alias == "kis_live" else f"{alias}|{ref}"


def _load_orders(export: Export) -> pd.DataFrame:
    frame = pd.concat(
        [export.tables[name] for name in LIVE_ORDER_TABLES], ignore_index=True
    )
    out = pd.DataFrame(
        {
            "alias": frame["account_alias"],
            "market": frame["market"],
            "symbol": [
                normalize_symbol(m, s)
                for m, s in zip(frame["market"], frame["symbol"], strict=True)
            ],
            "side": frame["side"],
            "order_type": frame["order_type"],
            "operation": frame["operation_kind"],
            "qty": _num(frame["qty"]),
            "limit_price": _num(frame["limit_price"]),
            "row_filled": _num(frame["filled_qty"]).fillna(0.0),
            "row_fill_price": _num(frame["fill_price"]),
            "outcome_raw": frame["terminal_outcome"],
            "tag": frame["strategy_tag"],
            "order_ref": frame["order_ref"],
            "trading_day": frame["trading_day"],
            "created": [parse_ts(v) for v in frame["created_at_utc"]],
            "filled_at": [parse_ts(v) for v in frame["filled_at_utc"]],
            "accepted": frame["accepted_at_utc"] != "",
        }
    )
    out["key"] = [
        _key(a, r, d)
        for a, r, d in zip(
            out["alias"], out["order_ref"], out["trading_day"], strict=True
        )
    ]
    return out


def _nearest_kis_key(row, by_ref: dict[str, list[dict]]) -> str | None:
    """A KIS fill whose day differs from its order's by at most one day."""
    if not row.trading_day:
        return None
    best: tuple[int, str] | None = None
    filled = date.fromisoformat(row.trading_day)
    for order in by_ref.get(row.order_ref, ()):
        if order["symbol"] != row.symbol or order["side"] != row.side:
            continue
        gap = abs((date.fromisoformat(order["trading_day"]) - filled).days)
        if gap <= 1 and (best is None or gap < best[0]):
            best = (gap, order["key"])
    return None if best is None else best[1]


def _load_fills(export: Export) -> pd.DataFrame:
    frame = export.tables["execution_ledger"]
    frame = frame[
        frame["account_alias"].isin(LIVE_ALIASES)
        & (frame["superseded_duplicate"] == "false")
    ]
    out = pd.DataFrame(
        {
            "alias": frame["account_alias"],
            "market": frame["market"],
            "symbol": [
                normalize_symbol(m, s)
                for m, s in zip(frame["market"], frame["symbol"], strict=True)
            ],
            "side": frame["side"],
            "qty": _num(frame["filled_qty"]),
            "price": _num(frame["fill_price"]),
            "source": frame["fill_source"],
            "order_ref": frame["order_ref"],
            "trading_day": frame["trading_day"],
            "ts": [parse_ts(v) for v in frame["filled_at_utc"]],
            "recorded": [parse_ts(v) for v in frame["created_at_utc"]],
        }
    ).reset_index(drop=True)
    out["seed"] = out["source"] == "manual_import"
    return out


def drop_cross_day_twins(fills: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Remove websocket rows that repeat a reconciler row, and undo their seed damage.

    The seed script computes ``seed = holdings - net ledger fills``, so a repeated sell
    recorded before the seed made that seed too large by exactly the repeated quantity.
    Returns the fills, the rows dropped and the seed rows corrected.
    """
    reconciler: dict[tuple, list[datetime]] = {}
    identity = ["alias", "order_ref", "side", "qty", "price"]
    for row in fills[fills["source"] == "reconciler"].to_dict("records"):
        reconciler.setdefault(tuple(row[c] for c in identity), []).append(row["ts"])

    fills = fills.copy()
    drop: list[int] = []
    corrected = 0
    websocket = fills[fills["source"] == "websocket"]
    for position, row in zip(
        websocket.index, websocket.to_dict("records"), strict=True
    ):
        stamps = reconciler.get(tuple(row[c] for c in identity), ())
        if not any(abs(stamp - row["ts"]) <= TWIN_MAX_GAP for stamp in stamps):
            continue
        drop.append(position)
        seeds = fills[
            (fills["source"] == "manual_import")
            & (fills["alias"] == row["alias"])
            & (fills["market"] == row["market"])
            & (fills["symbol"] == row["symbol"])
            & (fills["recorded"] > row["recorded"])
        ].sort_values("recorded", kind="stable")
        if len(seeds):
            first = seeds.index[0]
            fills.at[first, "qty"] = max(
                float(fills.at[first, "qty"]) - row["qty"], 0.0
            )
            corrected += 1
    return fills.drop(index=drop).reset_index(drop=True), len(drop), corrected


def drop_running_totals(
    fills: pd.DataFrame, order_qty: dict[str, float]
) -> tuple[pd.DataFrame, int, int]:
    """Resolve an order whose fills add up to more than it asked for.

    When one row equals the order's quantity, that row is the total and the others are
    the partial fills it already includes. Without such a row nothing can be proved, so
    the order is only counted. Returns the fills, rows dropped and orders unresolved.
    """
    drop: list[int] = []
    unresolved = 0
    real = fills[~fills["seed"] & fills["key"].notna()]
    for key, group in real.groupby("key", sort=True):
        wanted = order_qty.get(key)
        if wanted is None or group["qty"].sum() <= wanted * (1 + 1e-6):
            continue
        totals = group[(group["qty"] - wanted).abs() <= wanted * 1e-9]
        if len(totals) == 1:
            drop += [i for i in group.index if i != totals.index[0]]
        else:
            unresolved += 1
    return fills.drop(index=drop).reset_index(drop=True), len(drop), unresolved


def drop_inventory_proven_partials(
    fills: pd.DataFrame, order_keys: set[str]
) -> tuple[pd.DataFrame, int]:
    """Remove a running partial from an order with no order row, when inventory proves it.

    With no order quantity to test against, the proof is that the symbol's sells exceed
    everything it ever held by exactly this row's quantity, and that a later websocket row
    of the same order is larger. Real partial fills do not satisfy that by chance.
    """
    drop: list[int] = []
    real = fills[(fills["source"] == "websocket") & ~fills["key"].isin(order_keys)]
    for _key, group in real.groupby("key", sort=True):
        if len(group) < 2:
            continue
        first = group.iloc[0]
        held = fills[
            (fills["alias"] == first["alias"])
            & (fills["market"] == first["market"])
            & (fills["symbol"] == first["symbol"])
        ]
        excess = float(
            held.loc[held["side"] == "sell", "qty"].sum()
            - held.loc[held["side"] == "buy", "qty"].sum()
        )
        ordered = group.sort_values("ts", kind="stable")
        for position, (index, row) in enumerate(ordered.iterrows()):
            later = ordered.iloc[position + 1 :]
            if (
                row["qty"] > 0
                and abs(excess - row["qty"]) <= row["qty"] * 1e-6
                and (later["qty"] > row["qty"]).any()
            ):
                drop.append(index)
    return fills.drop(index=drop).reset_index(drop=True), len(drop)


def _market_counts(fills: pd.DataFrame) -> dict[str, int]:
    return {str(k): int(v) for k, v in fills.groupby("market").size().items()}


def _link_rungs(export: Export, orders: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Attach each rung to its order. For KIS, the order nearest the rung's update."""
    rungs = export.tables["order_proposal_rungs"]
    proposals = export.tables["order_proposals"].set_index("proposal_ref")
    by_ref: dict[tuple[str, str], list[int]] = {}
    for i, (alias, ref) in enumerate(
        zip(orders["alias"], orders["order_ref"], strict=True)
    ):
        by_ref.setdefault((alias, ref), []).append(i)

    columns: dict[str, list] = {
        "proposal_ref": [""] * len(orders),
        "rung_index": [None] * len(orders),
        "proposal_tag": [""] * len(orders),
        "exit_intent": [""] * len(orders),
    }
    unlinked = 0
    linked = rungs[rungs["order_ref"] != ""]
    for row in linked.itertuples(index=False):
        symbol = normalize_symbol(row.market, row.symbol)
        updated = parse_ts(row.updated_at_utc)
        candidates = [
            i
            for i in by_ref.get((row.account_alias, row.order_ref), ())
            if orders.at[i, "symbol"] == symbol and orders.at[i, "side"] == row.side
        ]
        if not candidates:
            unlinked += 1
            continue
        i = min(candidates, key=lambda c: (abs(orders.at[c, "created"] - updated), c))
        columns["proposal_ref"][i] = row.proposal_ref
        columns["rung_index"][i] = int(row.rung_index)
        if row.proposal_ref in proposals.index:
            parent = proposals.loc[row.proposal_ref]
            columns["proposal_tag"][i] = parent["strategy_tag"]
            columns["exit_intent"][i] = parent["exit_intent"]
    for name, values in columns.items():
        orders[name] = values
    return orders, unlinked


def _exported_market_counts(export: Export) -> dict[str, int]:
    frame = export.tables["execution_ledger"]
    frame = frame[frame["account_alias"].isin(LIVE_ALIASES)]
    return {str(k): int(v) for k, v in frame.groupby("market").size().items()}


def _keyed_over_quantity(
    loaded: pd.DataFrame,
    kis_by_ref: dict[str, list[dict]],
    order_qty: dict[str, float],
) -> int:
    """KIS orders whose raw fills exceed their quantity, before any cleaning.

    A repeated row carries a different day, so only the one-day fallback links it.
    """
    totals: dict[str, float] = {}
    for row in loaded[(loaded["alias"] == "kis_live") & ~loaded["seed"]].itertuples(
        index=False
    ):
        key = _key(row.alias, row.order_ref, row.trading_day)
        if key not in order_qty:
            key = _nearest_kis_key(row, kis_by_ref) or key
        totals[key] = totals.get(key, 0.0) + row.qty
    return sum(
        1
        for key, total in totals.items()
        if key in order_qty and total > order_qty[key] * (1 + 1e-6)
    )


def build_canon(export: Export, cleaning: Cleaning | None = None) -> Canon:
    cleaning = cleaning or Cleaning()
    orders = _load_orders(export)
    loaded = _load_fills(export)
    stages = {
        "exported_rows": _exported_market_counts(export),
        "unflagged_rows": _market_counts(loaded),
        "seed_rows": _market_counts(loaded[loaded["seed"]]),
    }
    fills, twins_dropped, seeds_corrected = (
        drop_cross_day_twins(loaded) if cleaning.twins else (loaded, 0, 0)
    )
    stages["after_twins"] = _market_counts(fills)

    keys = set(orders["key"])
    kis_by_ref: dict[str, list[dict]] = {}
    for row in orders[orders["alias"] == "kis_live"].to_dict("records"):
        kis_by_ref.setdefault(row["order_ref"], []).append(row)

    fill_keys: list[str | None] = []
    fill_stamps = list(fills["ts"])
    created_by_key = dict(zip(orders["key"], orders["created"], strict=True))
    day_fallback = 0
    for position, row in enumerate(fills.itertuples(index=False)):
        if row.seed:
            fill_keys.append(None)
            continue
        key = _key(row.alias, row.order_ref, row.trading_day)
        if key not in keys and row.alias == "kis_live":
            near = _nearest_kis_key(row, kis_by_ref)
            if near is not None:
                key, day_fallback = near, day_fallback + 1
                # The order's own day is the one the runbook says to trust.
                fill_stamps[position] = created_by_key[near]
        fill_keys.append(key)
    fills["key"] = fill_keys
    fills["ts"] = fill_stamps
    order_qty = dict(zip(orders["key"], orders["qty"], strict=True))
    keyed_over = _keyed_over_quantity(loaded, kis_by_ref, order_qty)
    fills, totals_dropped, overfills_unresolved = (
        drop_running_totals(fills, order_qty) if cleaning.totals else (fills, 0, 0)
    )
    fills, partials_dropped = (
        drop_inventory_proven_partials(fills, keys)
        if cleaning.proven_partials
        else (fills, 0)
    )
    stages["after_totals"] = _market_counts(fills)

    real = fills[~fills["seed"]]
    ledger_qty = real.groupby("key")["qty"].sum()
    ledger_notional = (real["qty"] * real["price"]).groupby(real["key"]).sum()
    orders["ledger_qty"] = orders["key"].map(ledger_qty).fillna(0.0)

    residuals: list[dict] = []
    for row in orders.itertuples(index=False):
        extra = row.row_filled - row.ledger_qty
        if extra > 1e-6 and row.row_fill_price == row.row_fill_price:
            residuals.append(
                {
                    "alias": row.alias,
                    "market": row.market,
                    "symbol": row.symbol,
                    "side": row.side,
                    "qty": extra,
                    "price": row.row_fill_price,
                    "source": "order_ledger_only",
                    "order_ref": row.order_ref,
                    "trading_day": row.trading_day,
                    # The ledger has no time for these. All are DAY orders, so the
                    # creation time gives the right session.
                    "ts": row.created if pd.isna(row.filled_at) else row.filled_at,
                    "recorded": row.created,
                    "seed": False,
                    "key": row.key,
                }
            )
    if residuals and cleaning.order_only:
        fills = pd.concat([fills, pd.DataFrame(residuals)], ignore_index=True)

    stages["after_order_only"] = _market_counts(fills)
    orders["filled_qty"] = orders[["row_filled", "ledger_qty"]].max(axis=1)
    notional = orders["key"].map(ledger_notional).fillna(0.0)
    residual_notional = (orders["row_filled"] - orders["ledger_qty"]).clip(
        lower=0.0
    ) * orders["row_fill_price"].fillna(0.0)
    orders["fill_notional"] = notional + residual_notional

    def corrected(row) -> str:
        if row.filled_qty >= row.qty - 1e-6 and row.qty > 0:
            return "filled"
        if row.filled_qty > 1e-9:
            return "partial"
        return row.outcome_raw

    orders["outcome"] = [corrected(r) for r in orders.itertuples(index=False)]
    orders, rungs_unlinked = _link_rungs(export, orders)
    orders["source"] = ["proposal" if p else "session" for p in orders["proposal_ref"]]

    stale = (orders["row_filled"] < orders["ledger_qty"] - 1e-6) & (
        orders["outcome_raw"] == "open"
    )
    diagnostics = {
        "ledger_fill_rows": int(
            (~fills["seed"] & (fills["source"] != "order_ledger_only")).sum()
        ),
        "seed_rows": int(fills["seed"].sum()),
        "order_ledger_only_fills": len(residuals),
        "cross_day_twins_dropped": twins_dropped,
        "seeds_corrected_for_twins": seeds_corrected,
        "running_total_rows_dropped": totals_dropped,
        "overfilled_orders_unresolved": overfills_unresolved,
        "inventory_proven_partials_dropped": partials_dropped,
        "orders_over_quantity_before_cleaning_keyed": keyed_over,
        "stages": stages,
        "order_rows": len(orders),
        "open_rows_with_ledger_fills": int(stale.sum()),
        "kis_day_fallback_fills": day_fallback,
        "rungs_with_order": int(
            (export.tables["order_proposal_rungs"]["order_ref"] != "").sum()
        ),
        "rungs_unlinked": rungs_unlinked,
        "outcome_counts_raw": orders["outcome_raw"]
        .value_counts()
        .sort_index()
        .to_dict(),
        "outcome_counts_corrected": orders["outcome"]
        .value_counts()
        .sort_index()
        .to_dict(),
        "first_order_by_alias": {
            alias: str(session_date("crypto", group["created"].min()))
            for alias, group in orders.groupby("alias")
        },
    }
    return Canon(orders=orders, fills=fills, diagnostics=diagnostics)


def reclassify_implausible(
    fills: pd.DataFrame, book: PriceBook
) -> tuple[pd.DataFrame, list[dict]]:
    """Treat a fill priced far from its session's close as an opening lot, not a trade.

    The quantity is kept, because later holdings reconcile with it. The price looks
    like an old average cost booked as a fill, so it makes no markout and no cash
    flow, and its return runs from the close on that date, like a seed.
    """
    fills = fills.copy()
    flagged: list[dict] = []
    seed = fills["seed"].to_numpy(copy=True)
    source = fills["source"].to_numpy(copy=True)
    for position, row in enumerate(fills.itertuples(index=False)):
        if row.seed:
            continue
        bars = book.get(row.market, row.symbol)
        i = bars.idx_on_or_after(row.date) if bars is not None else None
        if i is None:
            continue
        deviation = row.price / float(bars.close[i]) - 1.0
        if abs(deviation) > IMPLAUSIBLE_DEVIATION:
            seed[position] = True
            source[position] = "implausible_price"
            flagged.append(
                {
                    "alias": row.alias,
                    "market": row.market,
                    "symbol": row.symbol,
                    "side": row.side,
                    "day": row.date,
                    "deviation": deviation,
                }
            )
    fills["seed"] = seed
    fills["source"] = source
    return fills, flagged


def apply_splits(
    fills: pd.DataFrame, book: PriceBook
) -> tuple[pd.DataFrame, list[tuple]]:
    """Put ledger quantities and prices in the bars' split-adjusted units."""
    fills = fills.copy()
    fills["date"] = [
        session_date(m, t) for m, t in zip(fills["market"], fills["ts"], strict=True)
    ]
    qty, price, touched = [], [], []
    for row in fills.itertuples(index=False):
        bars = book.get(row.market, row.symbol)
        factor = bars.split_factor_after(row.date) if bars is not None else 1.0
        if factor != 1.0:
            touched.append((row.market, row.symbol, factor))
        qty.append(row.qty * factor)
        price.append(row.price / factor)
    fills["qty"] = qty
    fills["price"] = price
    return fills, sorted(set(touched))
