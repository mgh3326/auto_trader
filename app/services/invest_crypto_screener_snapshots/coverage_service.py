from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invest_crypto_screener_snapshots.freshness import (
    DataState,
    classify_crypto_partition,
    today_crypto_snapshot_date,
)
from app.services.invest_crypto_screener_snapshots.repository import (
    InvestCryptoScreenerSnapshotsRepository,
)


@dataclass(frozen=True)
class CryptoCoverageReport:
    market: str
    asOf: dt.datetime
    latestPartitionDate: dt.date | None
    snapshotsInLatestPartition: int
    snapshotsStale: int
    lastComputedAt: dt.datetime | None
    dataState: DataState


async def build_crypto_coverage(session: AsyncSession) -> CryptoCoverageReport:
    now = dt.datetime.now(dt.UTC)
    today = today_crypto_snapshot_date(now)
    counts = await InvestCryptoScreenerSnapshotsRepository(session).coverage(
        today=today
    )
    state = classify_crypto_partition(
        latest_partition_date=counts.latest_partition_date,
        row_count=counts.latest_partition_count,
        last_computed_at=counts.last_computed_at,
        today=today,
        now=now,
    )
    return CryptoCoverageReport(
        market="crypto",
        asOf=now,
        latestPartitionDate=counts.latest_partition_date,
        snapshotsInLatestPartition=counts.latest_partition_count,
        snapshotsStale=counts.stale_count,
        lastComputedAt=counts.last_computed_at,
        dataState=state,
    )
