"""Prefect wrapper for US invest_screener_snapshots refresh (ROB-204).

The flow is importable only; no deployment is registered in this PR. Writes are
runtime-gated by INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED so accidental manual
runs remain dry-run unless an operator explicitly enables the Prefect worker env.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from prefect import flow, task

from app.core.config import settings
from app.jobs.invest_screener_snapshots import SnapshotBuildRequest, run_snapshot_build


def _result_to_dict(result) -> dict[str, Any]:
    return {
        "market": result.market,
        "symbolsResolved": result.symbols_resolved,
        "snapshotsBuilt": result.snapshots_built,
        "skipped": result.skipped,
        "committed": result.committed,
        "batches": result.batches,
        "startedAt": result.started_at.isoformat(),
        "finishedAt": result.finished_at.isoformat(),
        "snapshotDateDistribution": result.snapshot_date_distribution,
        "samples": [
            {
                "market": sample.market,
                "symbol": sample.symbol,
                "snapshotDate": sample.snapshot_date.isoformat(),
                "latestClose": sample.latest_close,
                "consecutiveUpDays": sample.consecutive_up_days,
                "weekChangeRate": sample.week_change_rate,
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }


async def run_us_snapshot_refresh(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    batch_size: int = 200,
    concurrency: int = 4,
    common_stocks_only: bool = True,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Run the US snapshot builder with env-gated commit behavior."""
    commit_enabled = bool(settings.invest_screener_snapshots_commit_enabled)
    result = await run_snapshot_build(
        SnapshotBuildRequest(
            market="us",
            limit=limit,
            all_symbols=all_symbols,
            batch_size=batch_size,
            concurrency=concurrency,
            commit=commit_enabled,
            common_stocks_only=common_stocks_only,
            today=today,
        )
    )
    return _result_to_dict(result)


@task(name="invest_screener_snapshots_us_refresh")
async def invest_screener_snapshots_us_task(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    batch_size: int = 200,
    concurrency: int = 4,
    common_stocks_only: bool = True,
) -> dict[str, Any]:
    return await run_us_snapshot_refresh(
        all_symbols=all_symbols,
        limit=limit,
        batch_size=batch_size,
        concurrency=concurrency,
        common_stocks_only=common_stocks_only,
    )


@flow(name="invest_screener_snapshots_us")
async def invest_screener_snapshots_us_flow(
    *,
    all_symbols: bool = True,
    limit: int | None = None,
    batch_size: int = 200,
    concurrency: int = 4,
    common_stocks_only: bool = True,
) -> dict[str, Any]:
    """Post-US-close refresh flow; deployment registration is deferred."""
    return await invest_screener_snapshots_us_task(
        all_symbols=all_symbols,
        limit=limit,
        batch_size=batch_size,
        concurrency=concurrency,
        common_stocks_only=common_stocks_only,
    )
