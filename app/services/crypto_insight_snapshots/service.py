from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_insight_snapshot import CryptoInsightSnapshot
from app.services.crypto_insight_snapshots.repository import (
    CryptoInsightSnapshotsRepository,
)


async def latest_crypto_insight_sources(
    session: AsyncSession,
    *,
    metrics: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    symbols: Sequence[str | None] | None = None,
    limit_per_metric: int = 1,
) -> list[CryptoInsightSnapshot]:
    """Read latest cached crypto insight snapshots for /invest view-models.

    This service intentionally stays DB-only. Live provider fallback remains in the
    existing market dashboard services until a separate UI contract asks for more.
    """
    return await CryptoInsightSnapshotsRepository(session).list_latest(
        metrics=metrics,
        providers=providers,
        symbols=symbols,
        limit_per_metric=limit_per_metric,
    )
