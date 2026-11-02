"""Compute every table of the evidence pack from the export, prices and dividends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from research.strategy_evidence import (
    averaging,
    entry_style,
    ladders,
    losscut,
    parking,
    rulesets,
)
from research.strategy_evidence import overlay as ov
from research.strategy_evidence.common import IDLE_CASH_YIELD, MARKETS
from research.strategy_evidence.dividends import load_dividends
from research.strategy_evidence.lots import all_paths
from research.trade_retro.report import Results
from research.trade_retro.run import compute as compute_retro

WINDOW_START = date(2026, 5, 1)
#: Thresholds named in advance for the overlays and the options table: the tier's
#: -8% floor, a middle value, and the -40% depth of today's loss-cut classification.
OVERLAY_CUTS = (0.08, 0.20, 0.40)
OVERLAY_ADDS = (0.08, 0.15)
#: Rule-set variants named in advance for the options table (all are in the grid).
OPTION_VARIANTS = ("A2", "A4", "B3")
PARKING_INSTRUMENT = {"kr": "459580", "us": "SGOV"}


@dataclass
class Evidence:
    retro: Results
    dividends_sha256: str
    tier: pd.DataFrame
    buys: pd.DataFrame
    held_majors: dict
    add_rule: pd.DataFrame
    losscut_rows: pd.DataFrame
    losscut: pd.DataFrame
    intents: pd.DataFrame
    styles: pd.DataFrame
    yields: dict
    parking_lots: pd.DataFrame
    ladder_rows: pd.DataFrame
    ladders: pd.DataFrame
    overlays: dict
    sweeps: dict
    signals: pd.DataFrame
    rules: dict
    trades: dict = field(default_factory=dict)
    index_window: dict = field(default_factory=dict)


def add_rule_table(paths: pd.DataFrame, book) -> pd.DataFrame:
    """The systematic 50% add at each episode's first touch, scored like the tier."""
    rows = []
    for threshold in OVERLAY_ADDS:
        trades = averaging.average_down_overlay(paths, threshold)
        for row in trades.itertuples(index=False):
            ret, index = averaging.forward_return(
                book, row.market, row.symbol, row.day, row.price, averaging.D20
            )
            bars = book.get(row.market, row.symbol)
            rows.append(
                {
                    "market": row.market,
                    "threshold": threshold,
                    "symbol": row.symbol,
                    "day": row.day,
                    "d20": ret,
                    "d20_index": index,
                    "end": float(bars.close[-1]) / row.price - 1.0,
                }
            )
    return pd.DataFrame(
        rows,
        columns=["market", "threshold", "symbol", "day", "d20", "d20_index", "end"],
    )


def compute(export_dir: Path, prices_dir: Path, dividends_dir: Path) -> Evidence:
    retro = compute_retro(Path(export_dir).expanduser(), Path(prices_dir).expanduser())
    book, fills, orders = retro.book, retro.fills, retro.orders
    dividends, dividends_sha = load_dividends(Path(dividends_dir))
    paths = all_paths(fills, book, retro.cutover)

    tier = averaging.tier_orders(orders, fills, book, retro.cutover)
    buys = averaging.buys_by_lot_state(
        retro.executions, orders, fills, book, retro.cutover
    )
    loss_rows = losscut.losscut_table(paths, fills, book)
    intents = entry_style.intent_table(orders, fills, book)
    ladder_rows = ladders.sell_orders(orders, fills, book)

    frames = ov.account_frames(fills, book, retro.cutover)
    overlay_trades = {"base": ov.EMPTY}
    for x in OVERLAY_CUTS:
        overlay_trades[f"cut-{x:.0%}"] = losscut.overlay_trades(paths, fills, book, x)
    for x in OVERLAY_ADDS:
        overlay_trades[f"add-{x:.0%}"] = averaging.average_down_overlay(paths, x)
    overlay_trades["near-market"] = entry_style.near_market_overlay(intents)
    overlay_trades["sell-now"] = ladders.sell_now_overlay(ladder_rows)
    overlays = {}
    for market in MARKETS:
        for name, trades in overlay_trades.items():
            sub = trades[trades["market"] == market] if len(trades) else trades
            if name != "base" and not len(sub):
                continue
            overlays[(market, name)] = ov.market_overlay(frames, sub, book, market)

    yields = {
        m: parking.instrument_yield(book, dividends, m, s, WINDOW_START)
        for m, s in PARKING_INSTRUMENT.items()
    }
    sweeps = {}
    for (alias, market), frame in sorted(frames.items()):
        if yields.get(market) is None:
            continue
        idle = IDLE_CASH_YIELD.get((alias, market), 0.0)
        for case, table in parking.COST_CASES.items():
            sweeps[(alias, market, case)] = ov.parking_sweep(
                frame, yields[market]["annualized"], idle, table[market]
            )

    sigs = rulesets.signals(orders, retro.executions, fills)
    rules, trades = {}, {}
    for market in MARKETS:
        for variant in rulesets.VARIANTS:
            run = rulesets.run_variant(sigs, book, market, variant)
            trades[(market, variant.name)] = run
            rules[(market, variant.name)] = rulesets.portfolio(run, book, market)

    index_window = {}
    for market in MARKETS:
        bars = book.benchmarks[market]
        i = bars.idx_on_or_after(WINDOW_START)
        index_window[market] = (
            float(bars.close[-1]) / float(bars.close[i]) - 1.0,
            bars.date_at(i),
            bars.date_at(len(bars) - 1),
        )

    return Evidence(
        retro=retro,
        dividends_sha256=dividends_sha,
        tier=tier,
        buys=buys,
        held_majors=averaging.held_majors_proxy(buys),
        add_rule=add_rule_table(paths, book),
        losscut_rows=loss_rows,
        losscut=losscut.summarize_losscut(loss_rows),
        intents=intents,
        styles=entry_style.summarize_styles(intents),
        yields=yields,
        parking_lots=parking.lot_verdicts(
            parking.parking_lots(retro.matches, book, dividends)
        ),
        ladder_rows=ladder_rows,
        ladders=ladders.summarize_ladders(ladder_rows),
        overlays=overlays,
        sweeps=sweeps,
        signals=sigs,
        rules=rules,
        trades=trades,
        index_window=index_window,
    )
