"""Operator job for bounded support-proximity snapshot builds."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.invest_screener_snapshots.support_proximity_builder import (
    build_support_proximity_snapshots,
)
from app.services.invest_screener_snapshots.support_proximity_policy import (
    DEFAULT_CANDIDATE_POOL_LIMIT,
    DEFAULT_CONCURRENCY,
    DEFAULT_MIN_MARKET_CAP_KRW,
    DEFAULT_MIN_TURNOVER_KRW,
    MAX_CANDIDATE_POOL_LIMIT,
)


@dataclass(frozen=True)
class SupportProximityBuildRequest:
    market: str = "kr"
    candidate_pool_limit: int = DEFAULT_CANDIDATE_POOL_LIMIT
    concurrency: int = DEFAULT_CONCURRENCY
    min_market_cap: Decimal = DEFAULT_MIN_MARKET_CAP_KRW
    min_turnover: Decimal = DEFAULT_MIN_TURNOVER_KRW
    commit: bool = False


@dataclass(frozen=True)
class SupportProximitySample:
    symbol: str
    snapshot_date: dt.date
    latest_close: str
    support_price: str
    support_kind: str | None
    support_strength: str | None
    dist_to_support_pct: str
    market_cap: str
    support_computed_at: dt.datetime


@dataclass(frozen=True)
class SupportProximityBuildResult:
    market: str
    source_partition_date: dt.date | None
    candidates_resolved: int
    snapshots_built: int
    supports_built: int
    skipped: int
    committed: bool
    started_at: dt.datetime
    finished_at: dt.datetime
    samples: tuple[SupportProximitySample, ...] = ()
    warnings: tuple[str, ...] = ()


def _sample_rows(payloads: tuple[Any, ...]) -> tuple[SupportProximitySample, ...]:
    with_support = [
        row
        for row in payloads
        if row.dist_to_support_pct is not None
        and row.support_price is not None
        and row.market_cap is not None
        and row.support_computed_at is not None
    ]
    with_support.sort(key=lambda row: (row.dist_to_support_pct, row.symbol))
    return tuple(
        SupportProximitySample(
            symbol=row.symbol,
            snapshot_date=row.snapshot_date,
            latest_close=str(row.latest_close),
            support_price=str(row.support_price),
            support_kind=row.support_kind,
            support_strength=row.support_strength,
            dist_to_support_pct=str(row.dist_to_support_pct),
            market_cap=str(row.market_cap),
            support_computed_at=row.support_computed_at,
        )
        for row in with_support[:10]
    )


async def run_support_proximity_build(
    request: SupportProximityBuildRequest,
    *,
    session_factory: Any = AsyncSessionLocal,
) -> SupportProximityBuildResult:
    """Build, and optionally persist, support snapshots (dry-run by default)."""

    market = request.market.strip().lower()
    if market != "kr":
        raise ValueError("support_proximity snapshot builds currently support KR only")
    if not 1 <= request.candidate_pool_limit <= MAX_CANDIDATE_POOL_LIMIT:
        raise ValueError(
            f"candidate_pool_limit must be between 1 and {MAX_CANDIDATE_POOL_LIMIT}"
        )
    if request.concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    started_at = dt.datetime.now(dt.UTC)
    warnings: list[str] = []
    async with session_factory() as session:
        batch = await build_support_proximity_snapshots(
            session,
            candidate_pool_limit=request.candidate_pool_limit,
            concurrency=request.concurrency,
            min_market_cap=request.min_market_cap,
            min_turnover=request.min_turnover,
            now=started_at,
        )
        if batch.source_partition_date is None:
            warnings.append(
                "no invest_screener_snapshots base partition; run the ordinary "
                "KR snapshot builder first"
            )
        elif not batch.candidates:
            warnings.append(
                "base partition found, but no active common-stock candidate had "
                "trusted normalized market cap and sufficient turnover"
            )
        if len(batch.payloads) < len(batch.candidates):
            warnings.append(
                f"skipped {len(batch.candidates) - len(batch.payloads)} candidate(s) "
                "with unavailable completed OHLCV"
            )

        if request.commit:
            repository = InvestScreenerSnapshotsRepository(session)
            for payload in batch.payloads:
                await repository.upsert(payload)
            await session.commit()

    finished_at = dt.datetime.now(dt.UTC)
    return SupportProximityBuildResult(
        market=market,
        source_partition_date=batch.source_partition_date,
        candidates_resolved=len(batch.candidates),
        snapshots_built=len(batch.payloads),
        supports_built=batch.support_count,
        skipped=max(0, len(batch.candidates) - len(batch.payloads)),
        committed=request.commit,
        started_at=started_at,
        finished_at=finished_at,
        samples=_sample_rows(batch.payloads),
        warnings=tuple(warnings),
    )
