"""Dry-run-first job runner for crypto_insight_snapshots rows.

No TaskIQ schedule is registered here; scheduler activation requires a separate
operator approval after dry-run evidence.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.db import AsyncSessionLocal
from app.models.crypto_insight_snapshot import CryptoInsightSnapshot
from app.services.crypto_insight_snapshots.builder import (
    DEFAULT_PROVIDERS,
    build_crypto_insight_snapshots,
)
from app.services.crypto_insight_snapshots.repository import (
    CryptoInsightSnapshotsRepository,
    CryptoInsightSnapshotUpsert,
)


@dataclass(frozen=True)
class CryptoInsightSnapshotSample:
    metric: str
    provider: str
    symbol: str | None
    value: Decimal | None
    unit: str | None
    snapshot_at: dt.datetime


@dataclass(frozen=True)
class CryptoInsightSnapshotRefreshResult:
    snapshots_built: int
    committed: bool
    started_at: dt.datetime
    finished_at: dt.datetime
    providers: tuple[str, ...]
    samples: tuple[CryptoInsightSnapshotSample, ...] = ()
    snapshot_at_distribution: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _sample(payload: CryptoInsightSnapshotUpsert) -> CryptoInsightSnapshotSample:
    return CryptoInsightSnapshotSample(
        metric=payload.metric,
        provider=payload.provider,
        symbol=payload.symbol,
        value=payload.value,
        unit=payload.unit,
        snapshot_at=payload.snapshot_at,
    )


async def _commit_payloads(payloads: list[CryptoInsightSnapshotUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await CryptoInsightSnapshotsRepository(session).upsert(payloads)
        await session.commit()


async def refresh_crypto_insight_snapshots(
    *,
    dry_run: bool = True,
    providers: list[str] | tuple[str, ...] | None = None,
    symbols: list[str] | tuple[str, ...] | None = None,
    limit: int | None = None,
    confirm: bool = False,
) -> CryptoInsightSnapshotRefreshResult:
    if not dry_run and not confirm:
        raise ValueError("confirm=True is required when dry_run=False")
    started_at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    provider_tuple = tuple(p.strip().lower() for p in (providers or ()) if p.strip())
    effective_providers = provider_tuple or DEFAULT_PROVIDERS
    symbol_tuple = tuple(s.strip().upper() for s in (symbols or ()) if s.strip())
    if limit is not None:
        symbol_tuple = symbol_tuple[: max(0, limit)]

    build = await build_crypto_insight_snapshots(
        now=started_at,
        providers=provider_tuple or None,
        symbols=symbol_tuple or None,
    )
    payloads = list(
        build.payloads[:limit]
        if limit is not None and not symbol_tuple
        else build.payloads
    )
    if not dry_run and payloads:
        await _commit_payloads(payloads)
    distribution = Counter(payload.snapshot_at.isoformat() for payload in payloads)
    finished_at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    return CryptoInsightSnapshotRefreshResult(
        snapshots_built=len(payloads),
        committed=not dry_run,
        started_at=started_at,
        finished_at=finished_at,
        providers=effective_providers,
        samples=tuple(_sample(payload) for payload in payloads[:10]),
        snapshot_at_distribution=dict(sorted(distribution.items())),
        warnings=build.warnings,
    )


__all__ = [
    "CryptoInsightSnapshot",
    "CryptoInsightSnapshotRefreshResult",
    "CryptoInsightSnapshotSample",
    "refresh_crypto_insight_snapshots",
]
