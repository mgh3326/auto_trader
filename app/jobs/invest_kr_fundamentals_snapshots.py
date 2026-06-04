from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.invest_kr_fundamentals_snapshots.builder import (
    build_kr_fundamentals_snapshots,
)
from app.services.invest_kr_fundamentals_snapshots.provider import (
    TvScreenerKrFundamentalsProvider,
)
from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
)


@dataclass(frozen=True)
class KrFundamentalsSnapshotBuildRequest:
    limit: int | None = 200
    all_symbols: bool = False
    commit: bool = False


async def run_kr_fundamentals_snapshot_build(
    request: KrFundamentalsSnapshotBuildRequest,
) -> dict[str, Any]:
    limit = None if request.all_symbols else request.limit
    async with AsyncSessionLocal() as session:
        result = await build_kr_fundamentals_snapshots(
            provider=TvScreenerKrFundamentalsProvider(),
            repository=InvestKrFundamentalsSnapshotsRepository(session),
            commit=request.commit,
            limit=limit,
        )
        if request.commit:
            await session.commit()
        else:
            await session.rollback()
        return result
