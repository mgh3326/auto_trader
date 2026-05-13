from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)


@dataclass(frozen=True)
class MomentumCoverageReport:
    market: str
    asOf: dt.date
    momentumEvents: int
    themeEvents: int
    lastMomentumSnapshotAt: dt.datetime | None
    lastThemeSnapshotAt: dt.datetime | None
    dataState: str
    emptyReason: str | None = None


async def build_momentum_coverage(
    db: AsyncSession, *, market: str = "kr", as_of: dt.date | None = None
) -> MomentumCoverageReport:
    today = as_of or dt.datetime.now(dt.UTC).date()
    if market != "kr":
        return MomentumCoverageReport(
            market=market,
            asOf=today,
            momentumEvents=0,
            themeEvents=0,
            lastMomentumSnapshotAt=None,
            lastThemeSnapshotAt=None,
            dataState="unsupported",
            emptyReason="naver_stock_supports_kr_only",
        )
    cov = await InvestMomentumEventSnapshotsRepository(db).coverage(as_of=today)
    total = cov.momentum_count + cov.theme_count
    state = "fresh" if total else "missing"
    return MomentumCoverageReport(
        market="kr",
        asOf=today,
        momentumEvents=cov.momentum_count,
        themeEvents=cov.theme_count,
        lastMomentumSnapshotAt=cov.last_momentum_snapshot_at,
        lastThemeSnapshotAt=cov.last_theme_snapshot_at,
        dataState=state,
        emptyReason=None if total else "no_naver_momentum_snapshots",
    )
