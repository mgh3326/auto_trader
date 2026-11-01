"""How much the headline moves under each data treatment that could be argued."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.trade_retro.canonical import (
    Cleaning,
    apply_splits,
    build_canon,
    reclassify_implausible,
)
from research.trade_retro.entry import build_executions, classify_orders
from research.trade_retro.export import Export
from research.trade_retro.idle import daily_account, summarize_idle
from research.trade_retro.positions import cutover_time, match_frame, reconstruct
from research.trade_retro.prices import PriceBook
from research.trade_retro.rules import lot_entry_types


@dataclass
class Scenario:
    name: str
    lots: pd.DataFrame
    cash_shares: dict[tuple[str, str], float] | None


def _lots_and_cash(
    export: Export,
    book: PriceBook,
    cleaning: Cleaning | None = None,
    naive_seeds: bool = False,
    keep_implausible: bool = False,
    older_first: bool = True,
    later_seeds_reset: bool = True,
) -> tuple[pd.DataFrame, dict[tuple[str, str], float]]:
    canon = build_canon(export, cleaning or Cleaning())
    fills, _ = apply_splits(canon.fills, book)
    if not keep_implausible:
        fills, _ = reclassify_implausible(fills, book)
    cutover = cutover_time(fills)
    if naive_seeds:
        # Only ``seed`` changes, so the embedded-buy rule still reads the seed's source.
        fills = fills.copy()
        fills["seed"] = False
    walk = reconstruct(
        fills,
        book,
        cutover=cutover,
        older_first=older_first,
        later_seeds_reset=later_seeds_reset,
    )
    matches = match_frame(walk, book)
    orders = classify_orders(canon.orders, book)
    executions = build_executions(fills, orders, book)
    cash: dict[tuple[str, str], float] = {}
    for (alias, market), _group in fills.groupby(["alias", "market"], sort=True):
        frame, _meta = daily_account(
            fills, book, alias, market, cutover, later_seeds_reset
        )
        cash[(alias, market)] = summarize_idle(frame)["cash_share"]
    return lot_entry_types(matches, executions), cash


def run_scenarios(export: Export, book: PriceBook) -> list[Scenario]:
    base_lots, base_cash = _lots_and_cash(export, book)
    scenarios = [Scenario("Base: every rule in this report", base_lots, base_cash)]
    for name, kwargs in (
        (
            "No duplicate removal: trust `superseded_duplicate` alone",
            {"cleaning": Cleaning(twins=False, totals=False, proven_partials=False)},
        ),
        (
            "Keep the fill proven a partial only by inventory",
            {"cleaning": Cleaning(proven_partials=False)},
        ),
        (
            "Drop fills found only on an order row",
            {"cleaning": Cleaning(order_only=False)},
        ),
        ("Keep fills that cannot be trades as trades", {"keep_implausible": True}),
        (
            "Naive: count opening lots as buys at their stated cost",
            {"naive_seeds": True},
        ),
        ("Plain FIFO: older holdings do not go first", {"older_first": False}),
        (
            "Later seeds added on top of the ledger, not as whole holdings",
            {"later_seeds_reset": False},
        ),
    ):
        lots, cash = _lots_and_cash(export, book, **kwargs)
        scenarios.append(Scenario(name, lots, cash))
    scenarios.append(
        Scenario(
            "Only lots whose order has an order row",
            base_lots[
                ~base_lots["entry_type"].isin(("external", "external_micro", "unknown"))
            ],
            None,
        )
    )
    scenarios.append(
        Scenario(
            "Without recurring micro-buys",
            base_lots[base_lots["entry_type"] != "external_micro"],
            None,
        )
    )
    return scenarios
