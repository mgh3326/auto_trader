from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.invest_kr_fundamentals_snapshots.builder import (
    build_kr_fundamentals_snapshots,
)
from app.services.invest_kr_fundamentals_snapshots.provider import (
    KR_FUNDAMENTALS_FULL_FETCH_MIN_LIMIT,
    KR_FUNDAMENTALS_FULL_FETCH_UNIVERSE_BUFFER,
    TvScreenerKrFundamentalsProvider,
)
from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
)
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
)
from app.services.snapshot_commit_guard import assert_min_coverage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KrFundamentalsSnapshotBuildRequest:
    limit: int | None = 200
    all_symbols: bool = False
    commit: bool = False


async def _fetch_limit_for_request(
    request: KrFundamentalsSnapshotBuildRequest,
    *,
    universe_count: int | None = None,
) -> int | None:
    if not request.all_symbols:
        return request.limit
    if universe_count is None:
        async with AsyncSessionLocal() as session:
            universe_count = await active_universe_count(session, market="kr")
    if universe_count and universe_count > 0:
        return max(
            KR_FUNDAMENTALS_FULL_FETCH_MIN_LIMIT,
            universe_count + KR_FUNDAMENTALS_FULL_FETCH_UNIVERSE_BUFFER,
        )
    logger.warning(
        "KR fundamentals full-universe fetch falling back to provider floor: "
        "active universe count is unavailable"
    )
    return None


async def run_kr_fundamentals_snapshot_build(
    request: KrFundamentalsSnapshotBuildRequest,
) -> dict[str, Any]:
    limit = await _fetch_limit_for_request(request)
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


async def run_kr_fundamentals_snapshot_build_guarded(
    request: KrFundamentalsSnapshotBuildRequest,
) -> dict[str, Any]:
    """Two-pass coverage-guarded commit for KR fundamentals snapshots.

    A no-commit pass first validates the provider can build enough rows relative
    to the active KR universe.  If the build is thin (for example the historical
    200-row default), ``assert_min_coverage`` raises ``PartialCommitBlocked`` and
    the committing pass never runs.  ``--allow-partial`` callers should route to
    ``run_kr_fundamentals_snapshot_build`` directly.
    """
    async with AsyncSessionLocal() as session:
        universe_count = await active_universe_count(session, market="kr")
    if universe_count <= 0:
        logger.warning(
            "KR fundamentals commit guard disabled: active universe count is 0 "
            "(coverage could not be verified)"
        )

    dry_request = replace(request, commit=False)
    dry = await run_kr_fundamentals_snapshot_build(dry_request)
    assert_min_coverage(
        int(dry.get("would_upsert") or 0),
        universe_count,
        market="kr",
        metric="fundamentals rows",
    )
    return await run_kr_fundamentals_snapshot_build(request)
