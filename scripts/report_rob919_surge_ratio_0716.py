#!/usr/bin/env python3
"""ROB-919: read-only 2026-07-16 threshold-selection report for the relative
trade-value surge ratio (invest_momentum_event_snapshots history).

Reconstructs the Tier B "role model" population directly from
``invest_momentum_event_snapshots`` (close change_rate in [4%, 15%),
market_cap >= 2,000억, trade_value >= 200억, KRX venue, excluding inverse
ETFs by name) -- the same definition used in the 2026-07-17 KR-theme research
note -- then, for a set of candidate surge-ratio thresholds, reports:

  * recall: for each Tier B symbol, the first 10-minute snapshot on
    2026-07-16 at which its relative trade-value surge ratio (current
    cumulative trade_value / average of the same time-of-day cumulative
    trade_value on the 5 prior trading days) crosses the threshold, if any.
  * false positives: among all OTHER symbols captured that day that also
    clear the 09:40 gate's market_cap/trade_value floor (>=2,000억 /
    >=100억) but do NOT belong to Tier B, how many also cross the threshold
    by 09:40 KST.

NEVER writes to the database. SELECT only, everywhere in this file --
read-only through the repository layer plus a couple of scoped read queries
for the Tier-B population reconstruction.

Usage:
    ENV_FILE=.env.prod uv run python -m scripts.report_rob919_surge_ratio_0716
    ENV_FILE=.env.prod uv run python -m scripts.report_rob919_surge_ratio_0716 \\
        --trading-date 2026-07-16 --thresholds 3,5,8
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_momentum_events.surge_ratio import (
    compute_trade_value_surge_ratio,
)

logger = logging.getLogger(__name__)

_KST_09_40_UTC_TIME = dt.time(0, 40)  # 09:40 KST == 00:40 UTC (no DST in KR)
_GATE_MARKET_CAP_FLOOR = Decimal("200000000000")  # 2,000억
_GATE_TRADE_VALUE_FLOOR = Decimal("10000000000")  # 100억 (09:40 gate floor)
_TIER_B_TRADE_VALUE_FLOOR = Decimal("20000000000")  # 200억 (Tier B population floor)
_INVERSE_ETF_NAME_MARKERS = ("인버스", "레버리지", "ETN", "KODEX 200")


@dataclass(frozen=True)
class SymbolDayRow:
    symbol: str
    name: str | None
    change_rate: Decimal
    market_cap: Decimal | None
    trade_value: Decimal | None


async def _last_snapshot_per_symbol(
    session: AsyncSession, *, trading_date: dt.date
) -> list[SymbolDayRow]:
    """One row per symbol: its last KRX-venue snapshot that trading day.

    Read-only. ``trade_type='KRX'`` scopes to KRX-venue-computed trade_value
    (NXT is tracked separately and not comparable 1:1 for this population).
    """
    result = await session.execute(
        text(
            """
            WITH last_snap AS (
                SELECT symbol, MAX(snapshot_at) AS last_at
                FROM invest_momentum_event_snapshots
                WHERE trading_date = :trading_date AND trade_type = 'KRX'
                GROUP BY symbol
            )
            SELECT DISTINCT ON (m.symbol)
                m.symbol, m.name, m.change_rate, m.market_cap, m.trade_value
            FROM invest_momentum_event_snapshots m
            JOIN last_snap ls ON ls.symbol = m.symbol AND ls.last_at = m.snapshot_at
            WHERE m.trading_date = :trading_date AND m.trade_type = 'KRX'
            ORDER BY m.symbol, m.trade_value DESC NULLS LAST
            """
        ),
        {"trading_date": trading_date},
    )
    return [
        SymbolDayRow(
            symbol=row.symbol,
            name=row.name,
            change_rate=row.change_rate,
            market_cap=row.market_cap,
            trade_value=row.trade_value,
        )
        for row in result.all()
        if row.change_rate is not None
    ]


def _is_inverse_etf(name: str | None) -> bool:
    return bool(name) and any(marker in name for marker in _INVERSE_ETF_NAME_MARKERS)


def _is_tier_b(row: SymbolDayRow) -> bool:
    return (
        Decimal("4") <= row.change_rate < Decimal("15")
        and row.market_cap is not None
        and row.market_cap >= _GATE_MARKET_CAP_FLOOR
        and row.trade_value is not None
        and row.trade_value >= _TIER_B_TRADE_VALUE_FLOOR
        and not _is_inverse_etf(row.name)
    )


def _passes_gate_floor(row: SymbolDayRow) -> bool:
    """09:40 gate's market_cap/trade_value floor, independent of change_rate band."""
    return (
        row.market_cap is not None
        and row.market_cap >= _GATE_MARKET_CAP_FLOOR
        and row.trade_value is not None
        and row.trade_value >= _GATE_TRADE_VALUE_FLOOR
    )


async def _distinct_snapshot_times(
    session: AsyncSession, *, trading_date: dt.date
) -> list[dt.datetime]:
    """All distinct KRX-venue snapshot_at cycles that trading day, ascending."""
    result = await session.execute(
        text(
            """
            SELECT DISTINCT snapshot_at
            FROM invest_momentum_event_snapshots
            WHERE trading_date = :trading_date AND trade_type = 'KRX'
            ORDER BY snapshot_at ASC
            """
        ),
        {"trading_date": trading_date},
    )
    return [row.snapshot_at for row in result.all()]


@dataclass(frozen=True)
class SymbolThresholdResult:
    symbol: str
    name: str | None
    first_breach_at: dict[int, dt.datetime | None]
    ratio_at_0940: float | None
    reason_at_0940: str | None


_NEAREST_TOLERANCE = dt.timedelta(minutes=1)


def _nearest_trade_value(
    rows: list[tuple[dt.datetime, Decimal | None]],
    *,
    target_at: dt.datetime,
    tolerance: dt.timedelta,
) -> Decimal | None:
    """In-memory equivalent of repository.get_symbol_trade_value_near_time.

    Kept in-process (rather than one DB round trip per snapshot) because this
    report evaluates every symbol at every 10-minute cycle of the day across
    6 trading dates -- a per-call query here would be ~76 symbols x 42
    snapshots x 6 dates round trips. One bulk fetch per symbol (see
    ``_fetch_symbol_trade_value_series``) plus this in-memory nearest-match
    keeps the same "closest snapshot_at within tolerance, max across
    same-instant surfaces" semantics the repository uses for production.
    """
    candidates = [
        (at, value)
        for at, value in rows
        if value is not None and abs(at - target_at) <= tolerance
    ]
    if not candidates:
        return None
    closest_at = min((at for at, _ in candidates), key=lambda at: abs(at - target_at))
    values_at_closest = [value for at, value in candidates if at == closest_at]
    return max(values_at_closest)


async def _fetch_symbol_trade_value_series(
    session: AsyncSession, *, symbol: str, trading_dates: list[dt.date]
) -> dict[dt.date, list[tuple[dt.datetime, Decimal | None]]]:
    """Bulk read-only fetch: every (snapshot_at, trade_value) row for
    ``symbol`` on KRX venue across ``trading_dates``, grouped by date."""
    result = await session.execute(
        text(
            """
            SELECT trading_date, snapshot_at, trade_value
            FROM invest_momentum_event_snapshots
            WHERE symbol = :symbol AND trade_type = 'KRX'
              AND trading_date = ANY(:trading_dates)
            """
        ),
        {"symbol": symbol, "trading_dates": trading_dates},
    )
    by_date: dict[dt.date, list[tuple[dt.datetime, Decimal | None]]] = {
        d: [] for d in trading_dates
    }
    for row in result.all():
        by_date.setdefault(row.trading_date, []).append(
            (row.snapshot_at, row.trade_value)
        )
    return by_date


async def _surge_ratios_over_day(
    session: AsyncSession,
    *,
    row: SymbolDayRow,
    trading_date: dt.date,
    recent_trading_dates: list[dt.date],
    snapshot_times: list[dt.datetime],
    snapshot_at_0940: dt.datetime,
    thresholds: list[int],
) -> SymbolThresholdResult:
    by_date = await _fetch_symbol_trade_value_series(
        session,
        symbol=row.symbol,
        trading_dates=[trading_date, *recent_trading_dates],
    )
    today_rows = by_date[trading_date]

    first_breach_at: dict[int, dt.datetime | None] = dict.fromkeys(thresholds)
    ratio_at_0940: float | None = None
    reason_at_0940: str | None = None

    for snapshot_at in snapshot_times:
        current = _nearest_trade_value(
            today_rows, target_at=snapshot_at, tolerance=_NEAREST_TOLERANCE
        )
        historical = [
            _nearest_trade_value(
                by_date[historical_date],
                target_at=dt.datetime.combine(
                    historical_date, snapshot_at.time(), tzinfo=dt.UTC
                ),
                tolerance=_NEAREST_TOLERANCE,
            )
            for historical_date in recent_trading_dates
        ]
        surge = compute_trade_value_surge_ratio(
            current_trade_value=current, historical_trade_values=historical
        )
        if snapshot_at == snapshot_at_0940:
            ratio_at_0940 = surge.ratio
            reason_at_0940 = surge.reason_code
        if surge.ratio is not None:
            for threshold in thresholds:
                if first_breach_at[threshold] is None and surge.ratio >= threshold:
                    first_breach_at[threshold] = snapshot_at

    return SymbolThresholdResult(
        symbol=row.symbol,
        name=row.name,
        first_breach_at=first_breach_at,
        ratio_at_0940=ratio_at_0940,
        reason_at_0940=reason_at_0940,
    )


def _to_kst(at: dt.datetime | None) -> str:
    if at is None:
        return "no breach"
    kst = at.astimezone(dt.timezone(dt.timedelta(hours=9)))
    return kst.strftime("%H:%M KST")


def _print_report(
    *,
    trading_date: dt.date,
    thresholds: list[int],
    tier_b_results: list[SymbolThresholdResult],
    other_results: list[SymbolThresholdResult],
) -> None:
    print(
        f"\n=== ROB-919 surge-ratio threshold report ({trading_date.isoformat()}) ===\n"
    )
    print(
        f"Tier B population (momentum-snapshot-scoped): {len(tier_b_results)} symbols"
    )
    print(
        f"Other gate-floor symbols (candidate false positives): {len(other_results)}\n"
    )

    print("--- Tier B recall (first breach time, KST) + 09:40 ratio ---")
    header = (
        "symbol".ljust(10)
        + "name".ljust(14)
        + "".join(f"{t}x".rjust(12) for t in thresholds)
        + "ratio@0940".rjust(12)
        + "reason@0940".rjust(24)
    )
    print(header)
    for result in tier_b_results:
        line = result.symbol.ljust(10) + (result.name or "").ljust(14)
        for threshold in thresholds:
            line += _to_kst(result.first_breach_at[threshold]).rjust(12)
        ratio_label = (
            f"{result.ratio_at_0940:.2f}x"
            if result.ratio_at_0940 is not None
            else "n/a"
        )
        line += ratio_label.rjust(12) + " " + (result.reason_at_0940 or "-").rjust(28)
        print(line)

    print("\n--- Recall summary (breached by close) ---")
    for threshold in thresholds:
        hit = sum(1 for r in tier_b_results if r.first_breach_at[threshold] is not None)
        print(
            f"  {threshold}x: {hit}/{len(tier_b_results)} Tier B symbols breached "
            f"({hit / len(tier_b_results):.0%})"
            if tier_b_results
            else "  n/a"
        )

    print("\n--- False positives (breached by 09:40 KST, non-Tier-B) ---")
    for threshold in thresholds:
        fps = [
            r
            for r in other_results
            if r.first_breach_at[threshold] is not None
            and r.first_breach_at[threshold].time() <= _KST_09_40_UTC_TIME
        ]
        names = ", ".join(f"{r.symbol}({r.name})" for r in fps)
        print(f"  {threshold}x: {len(fps)} symbol(s) -- {names or 'none'}")

    computable_others = sorted(
        (r for r in other_results if r.ratio_at_0940 is not None),
        key=lambda r: r.ratio_at_0940,
        reverse=True,
    )
    print("\n--- Highest 09:40 ratios among non-Tier-B gate-floor symbols (top 5) ---")
    for r in computable_others[:5]:
        print(f"  {r.symbol} {r.name}: {r.ratio_at_0940:.2f}x")

    print("\n--- History coverage caveat (why recall may read as 0) ---")
    tier_b_computable = sum(1 for r in tier_b_results if r.ratio_at_0940 is not None)
    other_computable = sum(1 for r in other_results if r.ratio_at_0940 is not None)
    print(
        f"  Tier B: {tier_b_computable}/{len(tier_b_results)} symbols had a computable "
        "ratio at 09:40 (rest hit insufficient_history/missing_current_trade_value --"
        " i.e. they were not ranked at that time-of-day on enough of the prior 5"
        " trading days for invest_momentum_event_snapshots to supply a baseline)."
    )
    print(
        f"  Other gate-floor symbols: {other_computable}/{len(other_results)} had a "
        "computable ratio at 09:40."
    )
    print()


async def build_report(
    session: AsyncSession, *, trading_date: dt.date, thresholds: list[int]
) -> tuple[list[SymbolThresholdResult], list[SymbolThresholdResult]]:
    all_rows = await _last_snapshot_per_symbol(session, trading_date=trading_date)
    tier_b_rows = [row for row in all_rows if _is_tier_b(row)]
    tier_b_symbols = {row.symbol for row in tier_b_rows}
    other_rows = [
        row
        for row in all_rows
        if row.symbol not in tier_b_symbols and _passes_gate_floor(row)
    ]

    snapshot_times = await _distinct_snapshot_times(session, trading_date=trading_date)
    repo = InvestMomentumEventSnapshotsRepository(session)
    recent_trading_dates = await repo.list_recent_trading_dates(
        before_date=trading_date, limit=5
    )
    target_0940 = dt.datetime.combine(trading_date, _KST_09_40_UTC_TIME, tzinfo=dt.UTC)
    snapshot_at_0940 = min(snapshot_times, key=lambda at: abs(at - target_0940))

    tier_b_results = [
        await _surge_ratios_over_day(
            session,
            row=row,
            trading_date=trading_date,
            recent_trading_dates=recent_trading_dates,
            snapshot_times=snapshot_times,
            snapshot_at_0940=snapshot_at_0940,
            thresholds=thresholds,
        )
        for row in tier_b_rows
    ]
    other_results = [
        await _surge_ratios_over_day(
            session,
            row=row,
            trading_date=trading_date,
            recent_trading_dates=recent_trading_dates,
            snapshot_times=snapshot_times,
            snapshot_at_0940=snapshot_at_0940,
            thresholds=thresholds,
        )
        for row in other_rows
    ]
    return tier_b_results, other_results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only ROB-919 surge-ratio threshold report for a single "
            "trading date (never writes to the database)."
        )
    )
    parser.add_argument("--trading-date", default="2026-07-16")
    parser.add_argument("--thresholds", default="3,5,8")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="report-rob919-surge-ratio")
    args = parse_args(argv)
    trading_date = dt.date.fromisoformat(args.trading_date)
    thresholds = [int(t) for t in args.thresholds.split(",")]
    try:
        async with AsyncSessionLocal() as session:
            tier_b_results, other_results = await build_report(
                session, trading_date=trading_date, thresholds=thresholds
            )
        _print_report(
            trading_date=trading_date,
            thresholds=thresholds,
            tier_b_results=tier_b_results,
            other_results=other_results,
        )
        return 0
    except Exception:
        logger.exception("report_rob919_surge_ratio_0716 crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
