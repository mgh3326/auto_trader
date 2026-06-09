"""Prefect wrapper for KR fundamentals snapshot refresh (ROB-429 follow-up).

The KR Toss-parity fundamentals presets (undervalued_growth / growth_expectation /
stable_growth / cheap_value / steady_dividend / high_yield_value / undervalued_breakout)
read ``invest_kr_fundamentals_snapshots`` (tvscreener KR), with DART-first 3y-avg
growth when financial_fundamentals is backfilled. That snapshot had a build CLI +
job but **no Prefect flow** — so without a daily schedule it goes stale (observed
stuck at an old partition), and the "DART 미적재 근사치" path never refreshes. This
flow is the missing daily build trigger (mirrors the crypto / US screener flows).

Importable only; no deployment is registered in this PR. Writes are runtime-gated
by ``INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`` (shared with the KR/US/crypto
screener-snapshot flows) so accidental manual runs stay dry-run. ``allow_partial``
defaults False so the ROB-429 coverage guard still blocks a thin shadow commit;
operators opt in explicitly when a partial refresh is acceptable.
"""

from __future__ import annotations

from typing import Any

from prefect import flow, task

from app.core.config import settings
from app.jobs.invest_kr_fundamentals_snapshots import (
    KrFundamentalsSnapshotBuildRequest,
    run_kr_fundamentals_snapshot_build,
)


async def run_kr_fundamentals_refresh(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Run the KR fundamentals snapshot builder with env-gated commit behavior.

    ``commit`` comes from ``INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`` (default
    ``False`` → dry-run/rollback). The builder's ROB-429 coverage guard still
    applies; with ``allow_partial=False`` a thin partition is not committed.
    """
    commit_enabled = bool(settings.invest_screener_snapshots_commit_enabled)
    return await run_kr_fundamentals_snapshot_build(
        KrFundamentalsSnapshotBuildRequest(
            limit=limit,
            all_symbols=all_symbols,
            commit=commit_enabled,
            allow_partial=allow_partial,
        )
    )


@task(name="invest_kr_fundamentals_snapshots_refresh")
async def invest_kr_fundamentals_snapshots_task(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    allow_partial: bool = False,
) -> dict[str, Any]:
    return await run_kr_fundamentals_refresh(
        all_symbols=all_symbols, limit=limit, allow_partial=allow_partial
    )


@flow(name="invest_kr_fundamentals_snapshots")
async def invest_kr_fundamentals_snapshots_flow(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Daily KR fundamentals snapshot refresh; deployment registration deferred."""
    return await invest_kr_fundamentals_snapshots_task(
        all_symbols=all_symbols, limit=limit, allow_partial=allow_partial
    )
