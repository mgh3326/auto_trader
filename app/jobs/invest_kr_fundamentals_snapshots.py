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
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
)


@dataclass(frozen=True)
class KrFundamentalsSnapshotBuildRequest:
    limit: int | None = 200
    all_symbols: bool = False
    commit: bool = False
    allow_partial: bool = False


async def run_kr_fundamentals_snapshot_build(
    request: KrFundamentalsSnapshotBuildRequest,
) -> dict[str, Any]:
    limit = None if request.all_symbols else request.limit
    async with AsyncSessionLocal() as session:
        # ROB-429 A2: coverage denominator for the commit guard.
        universe_count = await active_universe_count(session, market="kr")
        result = await build_kr_fundamentals_snapshots(
            provider=TvScreenerKrFundamentalsProvider(),
            repository=InvestKrFundamentalsSnapshotsRepository(session),
            commit=request.commit,
            limit=limit,
            universe_count=universe_count,
            allow_partial=request.allow_partial,
        )
        # Only persist when the build actually committed (the guard may have
        # blocked a thin commit → result["committed"] is False → roll back).
        if result.get("committed"):
            await session.commit()
        else:
            await session.rollback()
        return result
