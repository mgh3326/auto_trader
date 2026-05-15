"""TaskIQ wrappers for invest screener snapshot activation.

No recurring schedule is attached intentionally. Operators can enqueue these tasks
manually after dry-run evidence/reviewer approval, or run them in dry-run mode to
produce an approval packet. Persisting rows requires commit=True.
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.taskiq_broker import broker
from app.jobs.invest_screener_snapshots import (
    SnapshotBuildRequest,
    run_snapshot_build,
)


@broker.task(task_name="build_invest_screener_snapshots")
async def build_invest_screener_snapshots(
    market: Literal["kr", "us"],
    symbols: list[str] | None = None,
    limit: int | None = 20,
    all_symbols: bool = False,
    batch_size: int = 200,
    concurrency: int = 4,
    common_stocks_only: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """Build invest_screener_snapshots rows, dry-run by default.

    commit=False returns counts/sample payload metadata without database writes.
    commit=True persists via the snapshot repository and should only be used after
    an operator/reviewer approval flow captured dry-run evidence.
    """
    request = SnapshotBuildRequest(
        market=market,
        symbols=tuple(symbols or ()),
        limit=limit,
        all_symbols=all_symbols,
        batch_size=batch_size,
        concurrency=concurrency,
        commit=commit,
        common_stocks_only=common_stocks_only,
    )
    result = await run_snapshot_build(request)
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
