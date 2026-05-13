from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.invest_crypto_screener_snapshots.builder import build_crypto_snapshots
from app.services.invest_crypto_screener_snapshots.provider import (
    TvScreenerUpbitCryptoSnapshotProvider,
)
from app.services.invest_crypto_screener_snapshots.repository import (
    InvestCryptoScreenerSnapshotsRepository,
)


@dataclass(frozen=True)
class CryptoSnapshotBuildRequest:
    limit: int | None = 50
    all_markets: bool = False
    commit: bool = False


async def run_crypto_snapshot_build(
    request: CryptoSnapshotBuildRequest,
) -> dict[str, Any]:
    limit = None if request.all_markets else request.limit
    async with AsyncSessionLocal() as session:
        result = await build_crypto_snapshots(
            provider=TvScreenerUpbitCryptoSnapshotProvider(),
            repository=InvestCryptoScreenerSnapshotsRepository(session),
            commit=request.commit,
            limit=limit,
        )
        if request.commit:
            await session.commit()
        else:
            await session.rollback()
        return result
