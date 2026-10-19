"""Per-candidate outcomes on the common clock: costs, benchmarks, markouts.

Unit: one candidate's **reference budget** ``B = q x P_ref`` -- the approved
whole-share quantity at the signal-time reference price (v1
``reference_price`` on ``D0``: open of the hourly bar containing the signal,
or the first hourly open after it). ``B`` is identical for every method of a
candidate, so methods are compared on the same money; unfilled shares stay
cash (0%), and a method that buys above ``P_ref`` simply invests a little
more than ``B``.

Exit = sell every filled share at the close of ``D0 + h`` (closing auction).

* ``status``: ``ok`` | ``unknown`` (a leg's fill is unknown) | ``immature``
  (``D0 + h`` has no bar yet) | ``data_missing``. Only ``ok`` rows enter the
  primary means; ``unknown`` rows also carry the two labelled bounds
  (unknown legs filled / unfilled). ``immature`` never becomes 0.
* Costs (``CostModel``): commission on both sides, sell tax on the exit
  notional (0 for ETFs), slippage on marketable entries and on every exit.
* Benchmark-adjusted: ``y - sum(leg notional / B x benchmark return over the
  leg's own holding interval)``. Market = KOSPI (^KS11) for ``.KS`` names,
  KOSDAQ (^KQ11) for ``.KQ``. Sector = equal-weight mean of same-sector peers
  (yfinance sector, peers = KR symbols in the price cache, self excluded,
  >= ``MIN_PEERS`` with prices at both ends) -- else missing, never 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from research.rob1383_buy_drought.core import tick_size
from research.rob1383_buy_drought.q2_q3_orders import reference_price
from research.rob1404_kr_entry.clock import (
    SessionCalendar,
    bar_state,
    close_on,
    price_at,
)
from research.rob1404_kr_entry.methods import FILLED, UNKNOWN, Leg, MethodLegs

HORIZONS = (5, 20)
MARKOUT_HORIZONS = (5, 20)
MIN_PEERS = 3


@dataclass(frozen=True, slots=True)
class CostModel:
    name: str
    commission: float  # per side, fraction of notional
    tax: float  # sell side, fraction of notional (stocks)
    slippage: str  # "none" | "tick" | "pct"
    slippage_pct: float = 0.0

    def slip(self, price: float) -> float:
        if self.slippage == "none":
            return 0.0
        if self.slippage == "tick":
            return tick_size("kr", price)
        if self.slippage == "pct":
            return price * self.slippage_pct
        raise ValueError(f"unknown slippage {self.slippage!r}")


# Base = account-observed values (Toss KR fills in toss_orders.json): commission
# ~0.015% per side, sell tax 0.20% on stocks / 0 on ETFs. Slippage unobserved.
BASE_COST = CostModel("base_c0.015_t0.20_s0", 0.00015, 0.0020, "none")
GROSS = CostModel("gross", 0.0, 0.0, "none")
COST_GRID = tuple(
    CostModel(f"c{c * 100:g}_t{t * 100:g}_s{sname}", c, t, skind, spct)
    for c in (0.0, 0.00015, 0.0025)
    for t in (0.0020, 0.0015)
    for sname, skind, spct in (
        ("0", "none", 0.0),
        ("1tick", "tick", 0.0),
        ("0.1pct", "pct", 0.001),
    )
)


@dataclass
class Bench:
    """Price access for one benchmark series (index or one peer)."""

    bars: pd.DataFrame
    hourly: pd.DataFrame | None

    def ret(self, at: pd.Timestamp, exit_date) -> float | None:
        p0 = price_at(self.bars, self.hourly, at)
        p1 = close_on(self.bars, exit_date)
        if p0 is None or p1 is None or not (math.isfinite(p0) and math.isfinite(p1)):
            return None
        if p0 <= 0:
            return None
        return p1 / p0 - 1.0


def leg_pnl(leg: Leg, exit_price: float, cost: CostModel, tax: float) -> float:
    entry = leg.price + (cost.slip(leg.price) if leg.marketable else 0.0)
    exit_ = exit_price - cost.slip(exit_price)
    n = leg.shares
    return (
        n * (exit_ - entry)
        - n * entry * cost.commission
        - n * exit_ * (cost.commission + tax)
    )


def _value(
    legs: tuple[Leg, ...],
    filled_mask: tuple[bool, ...],
    exit_price: float,
    budget: float,
    cost: CostModel,
    tax: float,
) -> float:
    return (
        sum(
            leg_pnl(leg, exit_price, cost, tax)
            for leg, f in zip(legs, filled_mask, strict=True)
            if f
        )
        / budget
    )


def _bench_adj(
    legs: tuple[Leg, ...],
    filled_mask: tuple[bool, ...],
    budget: float,
    exit_date,
    benches: list[Bench],
    min_peers: int,
) -> float | None:
    """Notional-weighted benchmark return over each filled leg's interval."""

    total = 0.0
    for leg, f in zip(legs, filled_mask, strict=True):
        if not f:
            continue
        if leg.at is None:
            return None
        rets = [r for b in benches if (r := b.ret(leg.at, exit_date)) is not None]
        if len(rets) < min_peers:
            return None
        total += leg.shares * leg.price / budget * (sum(rets) / len(rets))
    return total


def evaluate(
    m: MethodLegs,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    signal_at: Any,
    quantity: int,
    horizon: int,
    *,
    cal: SessionCalendar,
    is_etf: bool,
    market_bench: Bench | None,
    sector_peers: list[Bench] | None,
    costs: tuple[CostModel, ...] = (BASE_COST, GROSS),
) -> dict[str, Any]:
    """``D0`` and ``D0 + h`` are calendar sessions; the stock must have a bar on
    exactly those dates. A date past the last cached bar is ``immature``; a
    date inside the data with no bar is ``data_missing`` -- never shifted to
    the next row, never 0 (tester r1 B3)."""

    out: dict[str, Any] = {"method": m.method, "horizon": horizon}
    if m.missing:
        kind, _, reason = m.missing.partition(":")
        out["status"] = kind if kind in ("immature", "data_missing") else "data_missing"
        out["missing_reason"] = reason or m.missing
        return out
    i0 = cal.d0(signal_at)
    if i0 is None or i0 + horizon >= len(cal.dates):
        out["status"] = "immature"
        out["missing_reason"] = "calendar_end"
        return out
    d0, d0_state = bar_state(bars, cal.dates[i0])
    exit_pos, exit_state = bar_state(bars, cal.dates[i0 + horizon])
    for pos, state, what in ((d0, d0_state, "d0"), (exit_pos, exit_state, "exit")):
        if pos is None:
            out["status"] = "immature" if state == "beyond_data_end" else "data_missing"
            out["missing_reason"] = f"{what}_{state}"
            return out
    exit_price = float(bars["close"].iloc[exit_pos])
    if not math.isfinite(exit_price):
        out["status"] = "data_missing"
        out["missing_reason"] = "exit_nonfinite_close"
        return out
    p_ref, ref_basis = reference_price(bars, hourly, d0, signal_at)
    if not (math.isfinite(p_ref) and p_ref > 0):  # tester r2 B3
        out["status"] = "data_missing"
        out["missing_reason"] = "reference_nonfinite"
        return out
    budget = quantity * p_ref
    exit_date = bars["session_date"].iloc[exit_pos]
    states = tuple(leg.state for leg in m.legs)
    if any(
        leg.state == FILLED and leg.pos is not None and leg.pos > exit_pos
        for leg in m.legs
    ):
        raise ValueError("a fill after the evaluation close cannot be held to it")
    tax = 0.0 if is_etf else None
    out.update(
        {
            "d0_session": str(bars["session_date"].iloc[d0]),
            "exit_session": str(exit_date),
            "reference_price": p_ref,
            "reference_basis": ref_basis,
            "budget": budget,
            "n_legs": len(m.legs),
            "states": "|".join(states) if states else "none",
            "unknown_reasons": "|".join(
                leg.reason for leg in m.legs if leg.state == UNKNOWN
            ),
        }
    )
    known = tuple(s == FILLED for s in states)
    upper = tuple(s in (FILLED, UNKNOWN) for s in states)
    has_unknown = any(s == UNKNOWN for s in states)
    out["status"] = "unknown" if has_unknown else "ok"
    out["invested_frac"] = (
        sum(leg.shares * leg.price for leg, f in zip(m.legs, known, strict=True) if f)
        / budget
    )
    for cost in costs:
        t = cost.tax if tax is None else tax
        lo = _value(m.legs, known, exit_price, budget, cost, t)
        hi = _value(m.legs, upper, exit_price, budget, cost, t)
        key = f"y__{cost.name}"
        out[key] = None if has_unknown else lo
        out[f"{key}__if_unknown_unfilled"] = lo
        out[f"{key}__if_unknown_filled"] = hi
    for bench_name, benches, min_n in (
        ("mkt", [market_bench] if market_bench else [], 1),
        ("sector", sector_peers or [], MIN_PEERS),
    ):
        adj = _bench_adj(m.legs, known, budget, exit_date, benches, min_n)
        base = out[f"y__{BASE_COST.name}__if_unknown_unfilled"]
        out[f"y_{bench_name}adj__{BASE_COST.name}"] = (
            None if has_unknown or adj is None else base - adj
        )
        out[f"{bench_name}_adj_missing"] = bool(any(known)) and adj is None
    return out


def markouts(
    m: MethodLegs,
    bars: pd.DataFrame,
    market_bench: Bench | None,
    cal: SessionCalendar,
) -> list[dict[str, Any]]:
    """Post-fill price change: day 0 = fill session, close ``k`` *calendar*
    sessions later vs entry (gross). Missing/immature stay ``None`` with a status."""

    rows = []
    for i, leg in enumerate(m.legs):
        if leg.state != FILLED or leg.pos is None:
            continue
        fill_date = bars["session_date"].iloc[leg.pos]
        row: dict[str, Any] = {
            "method": m.method,
            "leg": i,
            "entry": leg.price,
            "fill_session": str(fill_date),
        }
        ci = next((j for j, d in enumerate(cal.dates) if d == fill_date), None)
        for k in MARKOUT_HORIZONS:
            row[f"markout{k}"] = None
            row[f"markout{k}_mktadj"] = None
            if ci is None:
                row[f"markout{k}_status"] = "fill_not_on_calendar"
                continue
            if ci + k >= len(cal.dates):
                row[f"markout{k}_status"] = "immature"
                continue
            pos, state = bar_state(bars, cal.dates[ci + k])
            if pos is None:
                row[f"markout{k}_status"] = (
                    "immature" if state == "beyond_data_end" else "data_missing"
                )
                continue
            close = float(bars["close"].iloc[pos])
            if not math.isfinite(close):
                row[f"markout{k}_status"] = "data_missing"  # tester r2 S1
                continue
            row[f"markout{k}_status"] = "ok"
            r = close / leg.price - 1.0
            row[f"markout{k}"] = r
            b = (
                market_bench.ret(leg.at, bars["session_date"].iloc[pos])
                if market_bench is not None and leg.at is not None
                else None
            )
            row[f"markout{k}_mktadj"] = None if b is None else r - b
        rows.append(row)
    return rows


def finite(x: Any) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)
