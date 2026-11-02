"""Question 4: parking instruments, net of costs, against cash left idle.

* Holdings: the retro's parking lots (FIFO matches), with holding days to their exit
  or to the last bar for a lot still open.
* Yield: price return plus cash distributions (ex-date inside the hold, not
  reinvested) over the export window, annualized on calendar days.
* Costs, two cases, both from ``config/trading_policy.yaml``:
  ``policy``: ``round_trip_cost_bps`` as written, the policy's conservative maximum
  (KR 47.4bp includes the 18bp stock sell tax; US 90bp includes 20bp FX spread per
  leg). ``etf``: the same table's commission alone (KR 14.7bp x 2; US 25bp x 2),
  because a listed ETF pays no KR sell tax and a USD purchase from USD cash needs no
  FX. Neither is a measured fee: the export has none.
* Idle cash earns the account's ``cash_yields`` rate (0% where none is listed).
* Break-even hold: the days after which yield above idle cash repays the round trip.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from research.strategy_evidence.common import IDLE_CASH_YIELD, ROUND_TRIP_COST
from research.trade_retro.canonical import is_parking
from research.trade_retro.prices import PriceBook

ETF_ROUND_TRIP = {"kr": 2 * 0.00147, "us": 2 * 0.0025}
COST_CASES = {"policy": ROUND_TRIP_COST, "etf": ETF_ROUND_TRIP}


def distributions(
    dividends: dict, market: str, symbol: str, after: date, through: date
) -> float:
    return sum(
        amount
        for day, amount in dividends.get((market, symbol), [])
        if after < day <= through
    )


def instrument_yield(
    book: PriceBook, dividends: dict, market: str, symbol: str, start: date
) -> dict | None:
    """Annualized total return from the first bar on or after ``start`` to the last."""
    bars = book.get(market, symbol)
    if bars is None:
        return None
    i = bars.idx_on_or_after(start)
    j = len(bars) - 1
    d0, d1 = bars.date_at(i), bars.date_at(j)
    days = (d1 - d0).days
    price = float(bars.close[j]) / float(bars.close[i]) - 1.0
    paid = distributions(dividends, market, symbol, d0, d1) / float(bars.close[i])
    total = price + paid
    return {
        "market": market,
        "symbol": symbol,
        "from": d0,
        "to": d1,
        "days": days,
        "price_return": price,
        "distribution_return": paid,
        "total_return": total,
        "annualized": total * 365.0 / days,
    }


def parking_lots(
    matches: pd.DataFrame, book: PriceBook, dividends: dict
) -> pd.DataFrame:
    """Each parking lot with its hold, price P&L and distributions received."""
    mask = np.array(
        [
            is_parking(m, s)
            for m, s in zip(matches["market"], matches["symbol"], strict=True)
        ],
        dtype=bool,
    )
    lots = matches[mask].copy()
    lots["paid"] = [
        row.qty
        * distributions(dividends, row.market, row.symbol, row.entry_day, row.exit_day)
        for row in lots.itertuples(index=False)
    ]
    lots["price_ret"] = lots["pnl_on_cost"] / lots["cost"]
    lots["dist_ret"] = lots["paid"] / lots["cost"]
    lots["total_ret"] = lots["price_ret"] + lots["dist_ret"]
    return lots


def lot_verdicts(lots: pd.DataFrame) -> pd.DataFrame:
    """Each lot's gross and net return against idle cash over the same days."""
    rows = []
    for row in lots.itertuples(index=False):
        idle = IDLE_CASH_YIELD.get((row.alias, row.market), 0.0) * row.days / 365.0
        record = {
            "market": row.market,
            "alias": row.alias,
            "symbol": row.symbol,
            "days": row.days,
            "realized": row.realized,
            "cost": row.cost,
            "total_ret": row.total_ret,
            "idle_ret": idle,
        }
        for case, table in COST_CASES.items():
            record[f"net_vs_idle_{case}"] = row.total_ret - table[row.market] - idle
        rows.append(record)
    columns = [
        "market",
        "alias",
        "symbol",
        "days",
        "realized",
        "cost",
        "total_ret",
        "idle_ret",
    ]
    return pd.DataFrame(
        rows, columns=columns + [f"net_vs_idle_{c}" for c in COST_CASES]
    )


def breakeven_days(annual_yield: float, idle_yield: float, round_trip: float) -> float:
    """Calendar days of holding after which the yield above idle repays the cost."""
    spread = annual_yield - idle_yield
    return float("inf") if spread <= 0 else round_trip / spread * 365.0
