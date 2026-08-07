"""Actual-side extraction — the operator's real 2025 KR trading.

Carries forward the GAP-02 selector and the GAP-03 censoring that the D3-C2P
fix-r1 job established and an independent verifier passed
(``carry_in_excluded_count = 4``, ``right_censored_count = 11``), and adds the
two pieces that job could not reach:

* the four session-derived metrics, now that the sealed 2025 XKRX calendar
  exists (``kospi_index_daily_2025.csv``, ``17e95d0b30ade5e6...``);
* ``capital_share``, whose GAP-04 definition needs a daily close mark per open
  position.

The cycle reconstruction, censoring, and medians all live in
``calibration_metrics``; this module only builds the fill tape and the daily
locked-share series. That is deliberate — the simulated side runs the exact
same code over its own fills.

GAP-05: the archive carries no usable fee. KIS domestic has no fee/tax column
at all, and every one of the 2,120 Toss KRW rows reports ``tax = 0`` including
188 executed sells, which KR transfer tax makes impossible. Fees are therefore
neither imputed nor modelled; both sides are compared gross.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.kr_corpus.d3_engine.calibration_metrics import (
    WINDOW_END,
    WINDOW_START,
    CycleFill,
    Reconstruction,
    SessionAxis,
    capital_share_observation,
    classify_gap03,
    compute_cycle_metrics,
    reconstruct_cycles,
)

KST = timezone(timedelta(hours=9))
DEFAULT_TRADE_HISTORY_ROOT = (
    Path.home() / "work" / "herdr-artifacts" / "operator-trade-history-v1" / "merged"
)
LOCKED_STREAK_SESSIONS = 180
SELECTOR_ID = "GAP-02: kis_domestic (all filled) UNION toss(currency==KRW, filled)"
CAPITAL_SHARE_DEFINITION_ID = "gap04_locked_share_daily_grain_time_weighted_mean"


class ActualSideInvalid(ValueError):
    code = "RUN_INVALID_CALIBRATION_ACTUAL_SIDE"


@dataclass(frozen=True, slots=True)
class ActualFill:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    day: date
    sequence: int
    account: str


def load_actual_fills(root: Path = DEFAULT_TRADE_HISTORY_ROOT) -> list[ActualFill]:
    """GAP-02 selector over the frozen operator archive."""

    import pandas as pd
    import pyarrow.parquet as pq

    kis = pq.read_table(str(root / "kis_domestic.parquet")).to_pandas()
    kis = kis.assign(
        qty=kis["tot_ccld_qty"].map(lambda value: Decimal(str(value).strip() or "0")),
        price=kis["avg_prvs"].map(lambda value: Decimal(str(value).strip() or "0")),
    )
    filled_kis = kis[kis["qty"] > 0].copy()
    filled_kis["ts"] = pd.to_datetime(
        filled_kis["ord_dt"] + filled_kis["ord_tmd"], format="%Y%m%d%H%M%S"
    ).dt.tz_localize(KST)
    filled_kis["symbol"] = filled_kis["pdno"]
    filled_kis["side"] = filled_kis["sll_buy_dvsn_cd"].map({"01": "SELL", "02": "BUY"})
    if filled_kis["side"].isna().any():
        raise ActualSideInvalid("unrecognized KIS sll_buy_dvsn_cd")

    toss = pq.read_table(str(root / "toss.parquet")).to_pandas()
    # The archive stores the execution block as a Python repr carrying Decimal
    # literals; builtins are stripped so only Decimal construction can run.
    executions = toss["execution"].map(
        lambda text: eval(text, {"__builtins__": {}, "Decimal": Decimal})  # noqa: S307
    )
    toss = toss.assign(
        qty=executions.map(lambda item: item["filledQuantity"]),
        price=executions.map(lambda item: item["averageFilledPrice"]),
        filled_at=executions.map(lambda item: item["filledAt"]),
    )
    filled_toss = toss[(toss["qty"] > 0) & (toss["currency"] == "KRW")].copy()
    filled_toss["ts"] = pd.to_datetime(filled_toss["filled_at"])
    filled_toss["side"] = filled_toss["side"].map(
        lambda value: str(value).strip().upper()
    )
    unknown = set(filled_toss["side"].unique()) - {"BUY", "SELL"}
    if unknown:
        raise ActualSideInvalid(f"unrecognized Toss side:{sorted(unknown)}")

    rows: list[tuple[Any, str, str, Decimal, Decimal, str]] = []
    for frame, account in ((filled_kis, "kis_domestic"), (filled_toss, "toss_krw")):
        for row in frame.itertuples(index=False):
            rows.append(
                (
                    row.ts,
                    str(row.symbol),
                    str(row.side),
                    Decimal(row.qty),
                    Decimal(row.price),
                    account,
                )
            )
    rows.sort(key=lambda item: (item[0], item[1]))
    return [
        ActualFill(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            day=timestamp.tz_convert(KST).date(),
            sequence=index,
            account=account,
        )
        for index, (timestamp, symbol, side, quantity, price, account) in enumerate(
            rows
        )
    ]


def to_cycle_fills(fills: list[ActualFill]) -> list[CycleFill]:
    return [
        CycleFill(
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            day=fill.day,
            sequence=fill.sequence,
        )
        for fill in fills
    ]


def daily_locked_share(
    fills: list[ActualFill],
    *,
    sessions: tuple[date, ...],
    closes: dict[str, dict[date, Decimal]],
    window_sessions: tuple[date, ...],
) -> tuple[list[Decimal], dict[str, Any]]:
    """GAP-04 locked share for the operator book, on the engine's own clock.

    Mirrors ``Position.apply_buy``/``apply_sell`` and
    ``policies.update_underwater_close`` exactly: the streak advances once per
    session on the session close, and resets whenever the close is missing or
    the mark is at/above the average price. Cost basis is gross (GAP-05); the
    simulated side's basis is ``gross * (1 + fee_rate)`` uniformly, so the two
    ratios remain directly comparable.
    """

    by_day: dict[date, list[ActualFill]] = {}
    for fill in fills:
        by_day.setdefault(fill.day, []).append(fill)
    for day_fills in by_day.values():
        day_fills.sort(key=lambda item: (item.sequence, item.symbol))

    quantity: dict[str, Decimal] = {}
    average: dict[str, Decimal] = {}
    cost_basis: dict[str, Decimal] = {}
    streak: dict[str, int] = {}
    unresolved: set[str] = set()
    pending: dict[date, list[ActualFill]] = dict(by_day)

    window = set(window_sessions)
    ratios: list[Decimal] = []
    observations: list[dict[str, object]] = []
    off_session_fill_days: set[date] = set()

    session_set = set(sessions)
    for day in sorted(pending):
        if day not in session_set:
            off_session_fill_days.add(day)

    for session in sessions:
        for fill in pending.get(session, ()):
            symbol = fill.symbol
            held = quantity.get(symbol, Decimal(0))
            if fill.side == "BUY":
                gross = fill.quantity * fill.price
                if held == 0:
                    quantity[symbol] = fill.quantity
                    average[symbol] = fill.price
                    cost_basis[symbol] = gross
                    streak[symbol] = 0
                else:
                    total = held + fill.quantity
                    average[symbol] = (held * average[symbol] + gross) / total
                    quantity[symbol] = total
                    cost_basis[symbol] = cost_basis[symbol] + gross
                continue
            if held <= 0:
                continue
            sold = min(fill.quantity, held)
            fraction = sold / held
            cost_basis[symbol] = cost_basis[symbol] * (Decimal(1) - fraction)
            quantity[symbol] = held - sold
            if quantity[symbol] == 0:
                average[symbol] = Decimal(0)
                cost_basis[symbol] = Decimal(0)
                streak[symbol] = 0

        for symbol, held in quantity.items():
            if held <= 0:
                streak[symbol] = 0
                continue
            close = closes.get(symbol, {}).get(session)
            if close is None:
                streak[symbol] = 0
                if symbol not in closes:
                    unresolved.add(symbol)
                continue
            streak[symbol] = streak.get(symbol, 0) + 1 if close < average[symbol] else 0

        if session not in window:
            continue
        invested = sum(
            (value for symbol, value in cost_basis.items() if quantity[symbol] > 0),
            Decimal(0),
        )
        locked = sum(
            (
                value
                for symbol, value in cost_basis.items()
                if quantity[symbol] > 0
                and streak.get(symbol, 0) >= LOCKED_STREAK_SESSIONS
            ),
            Decimal(0),
        )
        ratio = locked / invested if invested else Decimal(0)
        ratios.append(ratio)
        observations.append(
            {
                "session": session.isoformat(),
                "invested_cost_basis_krw": str(invested),
                "locked_cost_basis_krw": str(locked),
                "locked_share": str(ratio),
                "open_positions": sum(1 for value in quantity.values() if value > 0),
            }
        )

    diagnostics = {
        "clock_start_session": sessions[0].isoformat(),
        "observation_sessions": len(ratios),
        "symbols_without_corpus_closes": sorted(unresolved),
        "fill_days_off_xkrx_session": sorted(
            day.isoformat() for day in off_session_fill_days
        ),
        "daily_observations": observations,
    }
    return ratios, diagnostics


def build_actual_side(
    *,
    axis: SessionAxis,
    clock_sessions: tuple[date, ...],
    closes: dict[str, dict[date, Decimal]],
    root: Path = DEFAULT_TRADE_HISTORY_ROOT,
) -> dict[str, Any]:
    """The nine actual-side metrics plus their census and exclusion evidence."""

    fills = load_actual_fills(root)
    window_fills = [fill for fill in fills if WINDOW_START <= fill.day <= WINDOW_END]
    recon: Reconstruction = reconstruct_cycles(to_cycle_fills(fills))
    gap03 = classify_gap03(recon)
    metrics = compute_cycle_metrics(recon, gap03, axis)
    ratios, capital_diagnostics = daily_locked_share(
        fills,
        sessions=clock_sessions,
        closes=closes,
        window_sessions=axis.sessions,
    )
    metrics["capital_share"] = capital_share_observation(
        ratios,
        definition_id=CAPITAL_SHARE_DEFINITION_ID,
        note=(
            "GAP-04 locked share on the operator book; gross cost basis "
            "(GAP-05), clock seeded from the first fill of every position "
            "including carry-in"
        ),
    )
    census = metrics.pop("_census")
    return {
        "schema_id": "d3.calibration.actual_side.v2",
        "selector": SELECTOR_ID,
        "window": "2025-01-01..2025-12-31 (GAP-03 full-history censoring applied)",
        "session_axis": "sealed XKRX 2025 calendar (kospi_index_daily_2025.csv)",
        "fills_total_2025": len(window_fills),
        "fills_kis_domestic_2025": sum(
            1 for fill in window_fills if fill.account == "kis_domestic"
        ),
        "fills_toss_krw_2025": sum(
            1 for fill in window_fills if fill.account == "toss_krw"
        ),
        "symbols_with_activity_2025": len({fill.symbol for fill in window_fills}),
        "actual_fee_availability": {
            "kis_domestic": "absent_in_schema",
            "toss": "unpopulated",
        },
        "census": census,
        "capital_share_diagnostics": capital_diagnostics,
        "metrics": metrics,
    }
