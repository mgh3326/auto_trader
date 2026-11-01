"""FIFO position reconstruction.

A position is one account's holding in one symbol from flat back to flat. Each
piece of it is a ``Match``: a quantity with an entry and an exit, where an open
quantity's exit is the last daily close.

Three kinds of entry:

* ``fill``: a real buy inside the window.
* ``seed``: an opening-lot row (``manual_import``) or an implausible fill. Its price
  is an average cost from an unknown earlier date, and the base price is the last
  close complete at its stamp, so in-window P&L can be measured from there.
* ``inferred``: older holdings that must have existed, because a sell had nothing
  recorded to sell. They have no price, so a sell of them earns no return and is
  reported in its own bucket. They go first in the queue because they are the oldest,
  which keeps a sell of old shares from using up a lot bought in the window.

The seed script computed ``seed qty = holdings - net ledger fills since the
cutover`` (``opening_lots.py``). So a buy before the cutover of a seeded symbol is
already inside the seed and makes no lot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from research.trade_retro.canonical import reference_index
from research.trade_retro.prices import PriceBook


@dataclass
class Lot:
    seq: int
    qty: float
    price: float
    day: date
    origin: str
    key: str | None
    base: float
    index_day: date


@dataclass(frozen=True)
class Match:
    alias: str
    market: str
    symbol: str
    episode: int
    origin: str
    lot: int
    qty: float
    entry_price: float
    base_price: float
    entry_day: date
    index_day: date
    entry_key: str | None
    exit_price: float
    exit_day: date
    exit_key: str | None
    realized: bool


@dataclass(frozen=True)
class UnknownBasisSell:
    alias: str
    market: str
    symbol: str
    qty: float
    price: float
    day: date
    pre_cutover: bool


@dataclass
class Walk:
    matches: list[Match]
    unknown_sells: list[UnknownBasisSell]
    embedded_buys: int
    unpriced_open: list[tuple[str, str, str]]


def walk_symbol(
    rows: pd.DataFrame,
    cutover: datetime,
    mark: tuple[float, date] | None,
    seed_base: dict[date, tuple[float, date]] | None = None,
) -> Walk:
    """Walk one account's fills in one symbol. ``rows`` needs date, ts, side, qty,
    price, seed, source and key; ties sort buys before sells so a same-time pair
    matches. ``cutover`` is the instant the first seed batch is stamped with.
    ``seed_base`` maps a seed's date to its base price and that price's bar date."""
    first = rows.iloc[0]
    alias, market, symbol = first["alias"], first["market"], first["symbol"]
    has_seed = bool((rows["source"] == "manual_import").any())
    ordered = rows.assign(
        _rank=[0 if s == "buy" else 1 for s in rows["side"]]
    ).sort_values(["ts", "_rank"], kind="stable")

    def makes_lot(row) -> bool:
        return row.seed or not (has_seed and row.ts < cutover)

    balance = low = 0.0
    for row in ordered.itertuples(index=False):
        if row.side == "sell":
            balance -= row.qty
        elif makes_lot(row):
            balance += row.qty
        low = min(low, balance)

    lots: deque[Lot] = deque()
    next_seq = 0
    if low < -1e-12:
        day = ordered.iloc[0]["date"]
        lots.append(
            Lot(next_seq, -low, float("nan"), day, "inferred", None, float("nan"), day)
        )
        next_seq += 1

    matches: list[Match] = []
    unknown: list[UnknownBasisSell] = []
    embedded = 0
    episode = 0

    for row in ordered.itertuples(index=False):
        if row.side == "buy":
            if not makes_lot(row):
                embedded += 1
                continue
            if row.seed:
                base, index_day = (seed_base or {}).get(row.date, (row.price, row.date))
                lots.append(
                    Lot(
                        next_seq,
                        row.qty,
                        row.price,
                        row.date,
                        "seed",
                        None,
                        base,
                        index_day,
                    )
                )
            else:
                lots.append(
                    Lot(
                        next_seq,
                        row.qty,
                        row.price,
                        row.date,
                        "fill",
                        row.key,
                        row.price,
                        row.date,
                    )  # fmt: skip
                )
            next_seq += 1
            continue

        remaining = row.qty
        while remaining > 1e-12 and lots:
            lot = lots[0]
            taken = min(lot.qty, remaining)
            if lot.origin == "inferred":
                unknown.append(
                    UnknownBasisSell(
                        alias,
                        market,
                        symbol,
                        taken,
                        row.price,
                        row.date,
                        row.ts < cutover,
                    )
                )
            else:
                matches.append(
                    Match(
                        alias,
                        market,
                        symbol,
                        episode,
                        lot.origin,
                        lot.seq,
                        taken,
                        lot.price,
                        lot.base,
                        lot.day,
                        lot.index_day,
                        lot.key,
                        row.price,
                        row.date,
                        row.key,
                        True,
                    )  # fmt: skip
                )
            lot.qty -= taken
            remaining -= taken
            if lot.qty <= 1e-12:
                lots.popleft()
        if not lots:
            episode += 1

    unpriced: list[tuple[str, str, str]] = []
    for lot in lots:
        if lot.origin == "inferred":
            continue
        if mark is None:
            unpriced.append((alias, market, symbol))
            continue
        matches.append(
            Match(
                alias,
                market,
                symbol,
                episode,
                lot.origin,
                lot.seq,
                lot.qty,
                lot.price,
                lot.base,
                lot.day,
                lot.index_day,
                lot.key,
                mark[0],
                mark[1],
                None,
                False,
            )  # fmt: skip
        )
    return Walk(matches, unknown, embedded, sorted(set(unpriced)))


def cutover_time(fills: pd.DataFrame) -> datetime:
    """The first seed batch's instant, which every broker shares.

    An instant and not a date, because the same instant is a different local date in
    New York than in Seoul.
    """
    return min(fills.loc[fills["source"] == "manual_import", "ts"])


def reconstruct(fills: pd.DataFrame, book: PriceBook) -> Walk:
    cutover = cutover_time(fills)
    all_matches: list[Match] = []
    all_unknown: list[UnknownBasisSell] = []
    embedded = 0
    unpriced: list[tuple[str, str, str]] = []
    for (_alias, market, symbol), rows in fills.groupby(
        ["alias", "market", "symbol"], sort=True
    ):
        bars = book.get(market, symbol)
        mark = None
        seed_base: dict[date, tuple[float, date]] = {}
        if bars is not None and len(bars):
            mark = (float(bars.close[-1]), bars.date_at(len(bars) - 1))
            for row in rows[rows["seed"]].itertuples(index=False):
                i = reference_index(bars, market, row.ts)
                if i is not None:
                    seed_base[row.date] = (float(bars.close[i]), bars.date_at(i))
        walk = walk_symbol(rows, cutover, mark, seed_base)
        all_matches += walk.matches
        all_unknown += walk.unknown_sells
        embedded += walk.embedded_buys
        unpriced += walk.unpriced_open
    return Walk(all_matches, all_unknown, embedded, unpriced)


def match_frame(walk: Walk, book: PriceBook) -> pd.DataFrame:
    rows = []
    for m in walk.matches:
        rows.append(
            {
                "alias": m.alias,
                "market": m.market,
                "symbol": m.symbol,
                "episode": m.episode,
                "origin": m.origin,
                "lot": f"{m.alias}|{m.market}|{m.symbol}|{m.lot}",
                "qty": m.qty,
                "entry_day": m.entry_day,
                "exit_day": m.exit_day,
                "entry_key": m.entry_key,
                "exit_key": m.exit_key,
                "realized": m.realized,
                "cost": m.qty * m.entry_price,
                "base_cost": m.qty * m.base_price,
                "pnl_on_cost": m.qty * (m.exit_price - m.entry_price),
                "pnl": m.qty * (m.exit_price - m.base_price),
                "days": (m.exit_day - m.entry_day).days,
                "index_ret": book.index_return(m.market, m.index_day, m.exit_day),
                "index_ret_open": book.index_return_from_open(
                    m.market, m.index_day, m.exit_day
                ),
            }
        )
    frame = pd.DataFrame(rows)
    frame["index_pnl"] = frame["base_cost"] * frame["index_ret"].astype(float)
    frame["index_pnl_open"] = frame["base_cost"] * frame["index_ret_open"].astype(float)
    return frame


def position_frame(matches: pd.DataFrame) -> pd.DataFrame:
    """One row per position and origin. Averages are weighted by base cost."""

    def weighted(group: pd.DataFrame, column: str) -> float:
        return float(
            (group[column] * group["base_cost"]).sum() / group["base_cost"].sum()
        )

    rows = []
    keys = ["alias", "market", "symbol", "episode", "origin"]
    for key, group in matches.groupby(keys, sort=True):
        realized = group[group["realized"]]
        still_open = group[~group["realized"]]
        cost = float(group["base_cost"].sum())
        rows.append(
            dict(
                zip(keys, key, strict=True),
                base_cost=cost,
                cost=float(group["cost"].sum()),
                pnl=float(group["pnl"].sum()),
                pnl_on_cost=float(group["pnl_on_cost"].sum()),
                realized_pnl=float(realized["pnl"].sum()),
                realized_cost=float(realized["base_cost"].sum()),
                unrealized_pnl=float(still_open["pnl"].sum()),
                unrealized_cost=float(still_open["base_cost"].sum()),
                ret=float(group["pnl"].sum()) / cost if cost else 0.0,
                days=weighted(group, "days") if cost else 0.0,
                index_ret=weighted(group, "index_ret") if cost else 0.0,
                index_pnl=float(group["index_pnl"].sum()),
                is_open=not still_open.empty,
                opened=group["entry_day"].min(),
                closed=group["exit_day"].max(),
            )
        )
    return pd.DataFrame(rows)
