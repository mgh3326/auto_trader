from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.freshness import (
    DataState,
    expected_baseline_date,
)


@dataclass(frozen=True)
class CoverageReport:
    market: str
    asOf: dt.datetime
    totalSymbolsInUniverse: int
    snapshotsCoveringToday: int
    snapshotsStale: int
    snapshotsMissing: int
    lastComputedAt: dt.datetime | None
    dataState: DataState


async def build_coverage(session: AsyncSession, *, market: str) -> CoverageReport:
    # ROB-438 follow-up: coverage must classify against the session-aware baseline
    # (prior trading day during the pre-market window), matching the snapshot
    # loaders (ROB-281). today_trading_date() returned today's calendar trading day,
    # so a fresh prior-day partition was false-flagged stale/missing in pre-market.
    today = expected_baseline_date(market)
    now = dt.datetime.now(dt.UTC)

    if market == "kr":
        from app.models.kr_symbol_universe import KRSymbolUniverse

        universe_count_stmt = (
            sa.select(sa.func.count())
            .select_from(KRSymbolUniverse)
            .where(KRSymbolUniverse.is_active.is_(True))
        )
    else:
        from app.models.us_symbol_universe import USSymbolUniverse

        universe_count_stmt = (
            sa.select(sa.func.count())
            .select_from(USSymbolUniverse)
            .where(USSymbolUniverse.is_active.is_(True))
        )

    universe_count = int((await session.execute(universe_count_stmt)).scalar() or 0)

    stmt = sa.select(
        sa.func.count()
        .filter(InvestScreenerSnapshot.snapshot_date == today)
        .label("fresh"),
        sa.func.count()
        .filter(InvestScreenerSnapshot.snapshot_date < today)
        .label("stale"),
        sa.func.max(InvestScreenerSnapshot.computed_at).label("last"),
    ).where(InvestScreenerSnapshot.market == market)
    row = (await session.execute(stmt)).one()
    fresh = int(row.fresh or 0)
    stale = int(row.stale or 0)
    missing = max(0, universe_count - fresh - stale)

    if fresh == 0 and stale == 0:
        state: DataState = "missing"
    elif missing > 0 and fresh > 0:
        state = "fallback"
    elif stale > 0 and fresh == 0:
        state = "stale"
    elif stale > 0:
        # mixed fresh + stale: worst-of
        state = "stale"
    else:
        state = "fresh"

    return CoverageReport(
        market=market,
        asOf=now,
        totalSymbolsInUniverse=universe_count,
        snapshotsCoveringToday=fresh,
        snapshotsStale=stale,
        snapshotsMissing=missing,
        lastComputedAt=row.last,
        dataState=state,
    )
