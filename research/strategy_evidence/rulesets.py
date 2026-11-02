"""From-scratch rule sets, replayed on the same signals and prices.

The signal stream is what the account chose to buy: every market, symbol and local
ISO week with an accepted buy order or a real buy fill (recurring crypto micro-buys
included), parking excluded, deduplicated across accounts. Each signal enters one
unit of notional at the first open after it, so the rules differ only in execution
and exit. The variant list is fixed here and every variant is reported, whichever way
it points.

* ``A`` near-market entry, fixed loss-cut L and profit target T on the close, time
  stop after 60 sessions.
* ``B`` systematic averaging down: one more unit at the first close 10% and 20% below
  entry (cap: two adds, three units), all units out at the first close 10% above the
  average cost. ``B1`` adds a 60-session time stop, ``B3`` a stop 35% below the
  average cost after the adds.
* ``C`` baselines: hold every signal to the last bar, or exactly 20 sessions.

A position still open at the last bar is marked at its close. Every unit pays the
full ``ROUND_TRIP_COST`` of its market. Portfolio figures treat every unit as fully
funded: capital is the peak notional deployed at cost on any day, equity is capital
plus cumulative P&L, and drawdown is equity's largest fall from its running peak.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.strategy_evidence.common import ROUND_TRIP_COST
from research.trade_retro.canonical import is_parking, next_open_session, session_date
from research.trade_retro.markouts import cluster_ci
from research.trade_retro.prices import Bars, PriceBook

TIME_CAP = 60


@dataclass(frozen=True)
class Variant:
    name: str
    family: str
    loss: float | None = None
    target: float | None = None
    time_cap: int | None = None
    adds: tuple[float, ...] = ()
    stop_after_adds: float | None = None
    target_on_avg: bool = False

    def describe(self) -> str:
        if self.family == "A":
            return (
                f"near-market entry, cut at -{self.loss:.0%}, take profit at "
                f"+{self.target:.0%}, time stop {self.time_cap} sessions"
            )
        if self.family == "B":
            text = (
                "near-market entry, add 1 unit at "
                + " and ".join(f"-{a:.0%}" for a in self.adds)
                + f" (cap {len(self.adds)} adds), exit all at +{self.target:.0%} "
                "over average cost"
            )
            if self.time_cap:
                text += f", time stop {self.time_cap} sessions"
            if self.stop_after_adds:
                text += f", stop at -{self.stop_after_adds:.0%} from average cost"
            return text
        return (
            "near-market entry, hold to the last bar"
            if self.time_cap is None
            else f"near-market entry, hold exactly {self.time_cap} sessions"
        )


VARIANTS = (
    Variant("A1", "A", loss=0.10, target=0.15, time_cap=TIME_CAP),
    Variant("A2", "A", loss=0.10, target=0.30, time_cap=TIME_CAP),
    Variant("A3", "A", loss=0.20, target=0.15, time_cap=TIME_CAP),
    Variant("A4", "A", loss=0.20, target=0.30, time_cap=TIME_CAP),
    Variant("B1", "B", target=0.10, time_cap=TIME_CAP, adds=(0.10, 0.20), target_on_avg=True),
    Variant("B2", "B", target=0.10, adds=(0.10, 0.20), target_on_avg=True),
    Variant("B3", "B", target=0.10, adds=(0.10, 0.20), stop_after_adds=0.35, target_on_avg=True),
    Variant("C1", "C"),
    Variant("C2", "C", time_cap=20),
)  # fmt: skip


def signals(
    orders: pd.DataFrame, executions: pd.DataFrame, fills: pd.DataFrame
) -> pd.DataFrame:
    """One row per market, symbol and local ISO week, at its earliest time."""
    rows = []
    accepted = orders[
        orders["is_accepted"] & (orders["side"] == "buy") & ~orders["parking"]
    ]
    for row in accepted.itertuples(index=False):
        rows.append((row.market, row.symbol, row.created))
    first_ts = fills[~fills["seed"]].groupby("key")["ts"].min()
    buys = executions[(executions["side"] == "buy") & ~executions["parking"]]
    for row in buys.itertuples(index=False):
        rows.append((row.market, row.symbol, first_ts[row.key]))
    frame = pd.DataFrame(rows, columns=["market", "symbol", "ts"])
    frame = frame[
        [
            not is_parking(m, s)
            for m, s in zip(frame["market"], frame["symbol"], strict=True)
        ]
    ]
    frame["week"] = [
        "{}-{}".format(*session_date(m, t).isocalendar()[:2])
        for m, t in zip(frame["market"], frame["ts"], strict=True)
    ]
    frame = frame.sort_values("ts", kind="stable").drop_duplicates(
        ["market", "symbol", "week"]
    )
    return frame.reset_index(drop=True)


@dataclass
class Unit:
    i: int
    price: float


@dataclass
class Trade:
    market: str
    symbol: str
    units: list[Unit]
    exit_i: int
    exit_price: float
    reason: str

    @property
    def cost(self) -> float:
        return float(len(self.units))

    @property
    def gross(self) -> float:
        """Sum over units of the return to the exit price, one unit of notional each."""
        return sum(self.exit_price / u.price - 1.0 for u in self.units)


def simulate(
    bars: Bars, i0: int, variant: Variant
) -> tuple[list[Unit], int, float, str]:
    """One signal entered at the open of session ``i0``."""
    entry = float(bars.open[i0])
    units = [Unit(i0, entry)]
    last = len(bars) - 1
    end = last if variant.time_cap is None else min(i0 + variant.time_cap, last)
    pending = list(variant.adds)
    for k in range(i0, end + 1):
        close = float(bars.close[k])
        while pending and close <= entry * (1.0 - pending[0]):
            units.append(Unit(k, close))
            pending.pop(0)
        avg = len(units) / sum(1.0 / u.price for u in units)
        if variant.family == "A":
            if close <= entry * (1.0 - variant.loss):
                return units, k, close, "loss-cut"
            if close >= entry * (1.0 + variant.target):
                return units, k, close, "target"
        elif variant.family == "B":
            if close >= avg * (1.0 + variant.target):
                return units, k, close, "target"
            if (
                variant.stop_after_adds
                and not pending
                and close <= avg * (1.0 - variant.stop_after_adds)
            ):
                return units, k, close, "loss-cut"
    if variant.time_cap is not None and i0 + variant.time_cap <= last:
        return units, end, float(bars.close[end]), "time"
    return units, last, float(bars.close[last]), "open at end"


def run_variant(
    sigs: pd.DataFrame, book: PriceBook, market: str, variant: Variant
) -> list[Trade]:
    trades = []
    for row in sigs[sigs["market"] == market].itertuples(index=False):
        bars = book.get(market, row.symbol)
        if bars is None:
            continue
        i0 = next_open_session(bars, market, row.ts)
        if i0 is None:
            continue
        units, exit_i, exit_price, reason = simulate(bars, i0, variant)
        trades.append(Trade(market, row.symbol, units, exit_i, exit_price, reason))
    return trades


def portfolio(trades: list[Trade], book: PriceBook, market: str) -> dict:
    """Fully funded unit portfolio on the market's benchmark calendar."""
    index = book.benchmarks[market]
    grid = index.dates
    deployed = np.zeros(len(grid))
    pnl = np.zeros(len(grid))
    cost = ROUND_TRIP_COST[market]
    index_pnl = 0.0
    returns, symbols, reasons = [], [], []
    for trade in trades:
        bars = book.get(market, trade.symbol)
        for unit in trade.units:
            for k in range(unit.i, trade.exit_i + 1):
                g = int(np.searchsorted(grid, bars.dates[k], side="left"))
                if g >= len(grid):
                    continue
                price = trade.exit_price if k == trade.exit_i else float(bars.close[k])
                value = price / unit.price - 1.0 - cost
                nxt = (
                    int(np.searchsorted(grid, bars.dates[k + 1], side="left"))
                    if k < trade.exit_i
                    else len(grid)
                )
                if k < trade.exit_i:
                    deployed[g:nxt] += 1.0
                else:
                    deployed[g] += 1.0  # held through the exit session's close
                pnl[g:nxt] += value
            ir = book.index_return_from_open(
                market, bars.date_at(unit.i), bars.date_at(trade.exit_i)
            )
            index_pnl += (ir or 0.0) - cost
        returns.append((trade.gross - cost * trade.cost) / trade.cost)
        symbols.append(trade.symbol)
        reasons.append(trade.reason)
    if not trades:
        return {"trades": 0}
    capital = float(deployed.max()) or 1.0
    equity = capital + pnl
    peak = np.maximum.accumulate(equity)
    units_total = sum(t.cost for t in trades)
    arr = np.array(returns)
    ci = cluster_ci(arr, np.array(symbols))
    return {
        "trades": len(trades),
        "symbols": len(set(symbols)),
        "units": units_total,
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "win_rate": float((arr > 0).mean()),
        "worst": float(arr.min()),
        "ci": ci,
        "return_per_unit": float(pnl[-1] / units_total),
        "index_per_unit": float(index_pnl / units_total),
        "return_on_capital": float(pnl[-1] / capital),
        "max_drawdown": float((equity / peak - 1.0).min()),
        "capital_units": capital,
        "avg_deployed_share": float(deployed.mean() / capital),
        "reasons": {r: reasons.count(r) for r in sorted(set(reasons))},
    }
