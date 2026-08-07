"""D3-C2P actual-side diagnostic extraction — operator real 2025 KR trading.

Reconstructs KR trading "cycles" (flat -> buy -> ... -> flat) from the sealed
`operator-trade-history-v1` archive under the GAP-02 selector
(``kis_domestic`` in full UNION ``toss`` rows with ``currency == "KRW"``), and
computes the closed-cycle-series metrics that do not require an XKRX
session_seq axis: ``adds_per_cycle``, ``add_sizing_multiple``, ``trim_share``,
``signed_realized_pnl_pct``, ``annualized_cycle_count``.

This is runner/loader code only (``d3-calibration-gap-closure-20260807.md``
repo policy): it does not touch the D3 engine, contract, or golden inputs,
and it never opens the sealed ``kr-corpus-v1`` holdout/calibration corpus —
that is ``calibration_corpus.py``'s exclusive concern.

## GAP-03 censoring (fix-r1, replacing the R1 job's non-compliant output)

GAP-03 requires classifying every reconstructed cycle against the calendar
window [2025-01-01, 2025-12-31] using **full trade history**, not a
window-filtered fill set:

* **left-censored ("carry-in")**: the cycle's first buy predates the window.
  Any such cycle that has *any* in-window event (a 2025-dated add or sell —
  including the sell that closes it, or a partial trim of a still-open
  position) is excluded from every closed-cycle-series metric below and
  counted in ``carry_in_excluded_count``. A left-censored cycle with zero
  in-window events never contributes to those metrics in the first place, so
  it is not counted (it was never going to be included or excluded).
* **right-censored**: the cycle is still open (position > 0) as of
  2025-12-31, judged **as of that date** — a cycle that later fully closes
  in 2026 is still right-censored at the 2025 window boundary. Every
  right-censored cycle is excluded from the closed-cycle-series metrics and
  counted in ``right_censored_count``, regardless of when it opened (a cycle
  can be both left- and right-censored at once — see
  ``carry_in_and_right_censored_overlap``).
* The eligible universe for the four closed-cycle-series metrics is
  therefore: cycles whose first buy falls on/after 2025-01-01 **and** that
  fully closed (position hit exactly zero) on/before 2025-12-31.

## Oversell-clamp convention (disclosed, unchanged from the R1 job)

The archive contains sell rows whose quantity exceeds the reconstructed
position for a handful of symbols (``022100``, ``253450``, ``003380``,
``035720``) — recorded sells exceeding recorded buys, most plausibly because
the true position predates the archive window (toss coverage starts
2024-11-07, kis 2020-05-19) or reflects an external transfer. Consistent
with the R1 job's convention, such a sell is **clamped**: the sell quantity
used for cycle bookkeeping is ``min(sell_qty, position)``, which closes the
cycle at that event. Every clamp is recorded in ``anomalies`` (also
disclosed if a *further* sell then lands on an already-flat position — a
"sell while flat" residual, also recorded rather than silently dropped).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 50

KST = timezone(timedelta(hours=9))
WINDOW_START = date(2025, 1, 1)
WINDOW_END = date(2025, 12, 31)

DEFAULT_TRADE_HISTORY_ROOT = Path(
    "/Users/mgh3326/work/herdr-artifacts/operator-trade-history-v1/merged"
)


def _in_window(value: date | None) -> bool:
    return value is not None and WINDOW_START <= value <= WINDOW_END


@dataclass(slots=True)
class Cycle:
    symbol: str
    first_buy_date: date
    n_buys: int = 1
    n_sells: int = 0
    closed: bool = False
    close_date: date | None = None

    @property
    def key(self) -> tuple[str, date]:
        return (self.symbol, self.first_buy_date)

    def open_at(self, as_of: date) -> bool:
        """True iff position was still open as of ``as_of`` (right-censoring
        check) -- a cycle that closes *after* ``as_of`` is still open as of
        that date even though it is not open forever."""
        if self.first_buy_date > as_of:
            return False
        return not self.closed or (
            self.close_date is not None and self.close_date > as_of
        )


@dataclass(slots=True)
class AddEvent:
    cycle_key: tuple[str, date]
    date: date
    multiple: Decimal


@dataclass(slots=True)
class SellEvent:
    cycle_key: tuple[str, date]
    date: date
    trim_share: Decimal
    pnl_pct: Decimal


@dataclass(slots=True)
class Fill:
    symbol: str
    side: str  # "BUY" | "SELL"
    qty: Decimal
    price: Decimal
    ts: Any  # pandas Timestamp, tz-aware
    date_kst: date
    account: str  # "kis_domestic" | "toss_krw"

    @property
    def amount(self) -> Decimal:
        return self.qty * self.price


def load_fills(root: Path = DEFAULT_TRADE_HISTORY_ROOT) -> list[Fill]:
    """GAP-02 selector: kis_domestic (all filled rows) UNION toss (currency==KRW, filled)."""
    import pandas as pd
    import pyarrow.parquet as pq

    kis = pq.read_table(str(root / "kis_domestic.parquet")).to_pandas()
    kis_qty = kis["tot_ccld_qty"].map(lambda s: Decimal(str(s).strip() or "0"))
    kis_price = kis["avg_prvs"].map(lambda s: Decimal(str(s).strip() or "0"))
    kis = kis.assign(qty=kis_qty, price=kis_price)
    kf = kis[kis["qty"] > 0].copy()
    kf["ts"] = pd.to_datetime(
        kf["ord_dt"] + kf["ord_tmd"], format="%Y%m%d%H%M%S"
    ).dt.tz_localize(KST)
    kf["symbol"] = kf["pdno"]
    kf["side"] = kf["sll_buy_dvsn_cd"].map({"01": "SELL", "02": "BUY"})
    if kf["side"].isna().any():
        raise ValueError(
            f"unrecognized sll_buy_dvsn_cd: {kf['sll_buy_dvsn_cd'].unique()}"
        )

    toss = pq.read_table(str(root / "toss.parquet")).to_pandas()
    execs = toss["execution"].map(lambda s: eval(s, {"Decimal": Decimal}))  # noqa: S307
    toss = toss.assign(
        qty=execs.map(lambda d: d["filledQuantity"]),
        price=execs.map(lambda d: d["averageFilledPrice"]),
        filled_at=execs.map(lambda d: d["filledAt"]),
    )
    tf = toss[(toss["qty"] > 0) & (toss["currency"] == "KRW")].copy()
    tf["ts"] = pd.to_datetime(tf["filled_at"])

    fills: list[Fill] = []
    for frame, account in ((kf, "kis_domestic"), (tf, "toss_krw")):
        for row in frame.itertuples(index=False):
            ts = row.ts
            fills.append(
                Fill(
                    symbol=str(row.symbol),
                    side=str(row.side),
                    qty=Decimal(row.qty),
                    price=Decimal(row.price),
                    ts=ts,
                    date_kst=ts.tz_convert(KST).date(),
                    account=account,
                )
            )
    fills.sort(key=lambda f: (f.ts, f.symbol))
    return fills


@dataclass(slots=True)
class Reconstruction:
    cycles: list[Cycle] = field(default_factory=list)
    add_events: list[AddEvent] = field(default_factory=list)
    sell_events: list[SellEvent] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def reconstruct_cycles(fills: list[Fill]) -> Reconstruction:
    """Full-history reconstruction, positions merged across accounts by
    symbol (a KR economic position is one exposure regardless of which
    broker leg executed it — GAP-02 already treats kis_domestic/toss as a
    single combined KR universe)."""
    by_symbol: dict[str, list[Fill]] = {}
    for f in fills:
        by_symbol.setdefault(f.symbol, []).append(f)

    out = Reconstruction()
    for symbol, symbol_fills in by_symbol.items():
        pos = Decimal(0)
        avg = Decimal(0)
        cyc: Cycle | None = None
        for f in symbol_fills:
            if f.side == "BUY":
                if pos == 0:
                    cyc = Cycle(symbol=symbol, first_buy_date=f.date_kst)
                    out.cycles.append(cyc)
                    avg = f.price
                    pos = f.qty
                else:
                    assert cyc is not None
                    out.add_events.append(
                        AddEvent(
                            cycle_key=cyc.key,
                            date=f.date_kst,
                            multiple=f.amount / _first_buy_amount(symbol_fills, cyc),
                        )
                    )
                    cyc.n_buys += 1
                    avg = (pos * avg + f.qty * f.price) / (pos + f.qty)
                    pos += f.qty
            else:  # SELL
                if pos <= 0:
                    out.anomalies.append(
                        f"SELL while flat: {symbol} {f.ts} qty={f.qty}"
                    )
                    continue
                sell_qty = f.qty
                if sell_qty > pos:
                    out.anomalies.append(
                        f"SELL qty>pos (clamped to position): {symbol} {f.ts} "
                        f"qty={f.qty} pos={pos}"
                    )
                    sell_qty = pos
                assert cyc is not None
                out.sell_events.append(
                    SellEvent(
                        cycle_key=cyc.key,
                        date=f.date_kst,
                        trim_share=sell_qty / pos,
                        pnl_pct=(f.price - avg) / avg * 100,
                    )
                )
                cyc.n_sells += 1
                pos -= sell_qty
                if pos == 0:
                    cyc.closed = True
                    cyc.close_date = f.date_kst
    return out


def _first_buy_amount(symbol_fills: list[Fill], cyc: Cycle) -> Decimal:
    for f in symbol_fills:
        if f.side == "BUY" and f.date_kst == cyc.first_buy_date:
            return f.amount
    raise AssertionError("cycle first buy not found in its own fill list")


@dataclass(slots=True)
class Gap03Classification:
    carry_in_closed: list[Cycle]
    carry_in_open_with_activity: list[Cycle]
    right_censored: list[Cycle]
    eligible_closed: list[Cycle]  # the closed-cycle-series universe

    @property
    def carry_in_all(self) -> list[Cycle]:
        return self.carry_in_closed + self.carry_in_open_with_activity

    @property
    def overlap_carry_in_and_right_censored(self) -> list[Cycle]:
        rc = {c.key for c in self.right_censored}
        return [c for c in self.carry_in_open_with_activity if c.key in rc]


def classify_gap03(recon: Reconstruction) -> Gap03Classification:
    closed_in_window = [
        c for c in recon.cycles if c.closed and _in_window(c.close_date)
    ]
    right_censored = [c for c in recon.cycles if c.open_at(WINDOW_END)]

    carry_in_closed = [c for c in closed_in_window if c.first_buy_date < WINDOW_START]

    activity_keys: set[tuple[str, date]] = set()
    for e in recon.add_events:
        if _in_window(e.date):
            activity_keys.add(e.cycle_key)
    for e in recon.sell_events:
        if _in_window(e.date):
            activity_keys.add(e.cycle_key)

    carry_in_open_with_activity = [
        c
        for c in right_censored
        if c.first_buy_date < WINDOW_START and c.key in activity_keys
    ]

    eligible_closed = [c for c in closed_in_window if c.first_buy_date >= WINDOW_START]

    return Gap03Classification(
        carry_in_closed=carry_in_closed,
        carry_in_open_with_activity=carry_in_open_with_activity,
        right_censored=right_censored,
        eligible_closed=eligible_closed,
    )


def _median(values: list[Decimal]) -> tuple[Decimal | None, int]:
    values = sorted(values)
    n = len(values)
    if n == 0:
        return None, 0
    if n % 2:
        return values[n // 2], n
    return (values[n // 2 - 1] + values[n // 2]) / 2, n


def compute_metrics(
    recon: Reconstruction, gap03: Gap03Classification
) -> dict[str, Any]:
    eligible_keys = {c.key for c in gap03.eligible_closed}

    adds = [Decimal(c.n_buys - 1) for c in gap03.eligible_closed]
    adds_median, adds_n = _median(adds)

    add_multiples = [
        e.multiple for e in recon.add_events if e.cycle_key in eligible_keys
    ]
    add_median, add_n = _median(add_multiples)

    sells = [e for e in recon.sell_events if e.cycle_key in eligible_keys]
    trim_median, trim_n = _median([e.trim_share for e in sells])
    pnl_median, pnl_n = _median([e.pnl_pct for e in sells])

    window_days = (WINDOW_END - WINDOW_START).days + 1
    annualized = Decimal(len(gap03.eligible_closed)) * Decimal("365.2425") / window_days

    return {
        "closed_cycle_count": len(gap03.eligible_closed),
        "right_censored_count": len(gap03.right_censored),
        "carry_in_excluded_count": len(gap03.carry_in_all),
        "carry_in_excluded_breakdown": {
            "closed_carry_in": sorted(c.symbol for c in gap03.carry_in_closed),
            "open_carry_in_with_2025_activity": sorted(
                c.symbol for c in gap03.carry_in_open_with_activity
            ),
        },
        "carry_in_and_right_censored_overlap": sorted(
            c.symbol for c in gap03.overlap_carry_in_and_right_censored
        ),
        "anomalies": list(recon.anomalies),
        "metrics": {
            "annualized_cycle_count": {
                "median_decimal": str(annualized),
                "n": 1,
                "note": (
                    "single window-level observation, GAP-01 calendar-day basis "
                    "(365.2425/window_calendar_days); numerator = GAP-03-eligible "
                    "closed_cycle_count"
                ),
            },
            "adds_per_cycle": {
                "median_decimal": str(adds_median) if adds_median is not None else None,
                "n": adds_n,
                "raw_observation_count": adds_n,
            },
            "add_sizing_multiple": {
                "median_decimal": str(add_median) if add_median is not None else None,
                "n": add_n,
                "raw_observation_count": add_n,
            },
            "trim_share": {
                "median_decimal": str(trim_median) if trim_median is not None else None,
                "n": trim_n,
                "raw_observation_count": trim_n,
            },
            "signed_realized_pnl_pct": {
                "median_decimal": str(pnl_median) if pnl_median is not None else None,
                "n": pnl_n,
                "raw_observation_count": pnl_n,
                "fee_basis": (
                    "gross (GAP-05: actual fee data absent/unpopulated in source schema)"
                ),
            },
        },
    }


def main(root: Path = DEFAULT_TRADE_HISTORY_ROOT) -> dict[str, Any]:
    fills = load_fills(root)
    window_fills = [f for f in fills if _in_window(f.date_kst)]
    result = {
        "schema_id": "d3.calibration.actual_side_diagnostic.v1",
        "selector": "GAP-02: kis_domestic (all) UNION toss(currency==KRW)",
        "window": "2025-01-01..2025-12-31 (calendar; GAP-03 full-history censoring applied)",
        "fills_total_2025": len(window_fills),
        "fills_kis_domestic_2025": sum(
            1 for f in window_fills if f.account == "kis_domestic"
        ),
        "fills_toss_krw_2025": sum(1 for f in window_fills if f.account == "toss_krw"),
        "symbols_with_activity": len({f.symbol for f in window_fills}),
    }
    recon = reconstruct_cycles(fills)
    gap03 = classify_gap03(recon)
    result.update(compute_metrics(recon, gap03))
    return result


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, ensure_ascii=False))
