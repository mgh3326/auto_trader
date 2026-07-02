"""TaskIQ wrappers for analyst consensus snapshot freshness (ROB-641).

No recurring schedule is attached intentionally. Operators can enqueue this task
manually in dry-run mode to produce an approval packet. Persisting rows requires
commit=True and separate production database-write approval.
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.taskiq_broker import broker
from app.jobs.analyst_consensus_snapshots import (
    AnalystConsensusSnapshotBuildRequest,
    run_analyst_consensus_snapshot_build,
)


@broker.task(task_name="build_analyst_consensus_snapshots")
async def build_analyst_consensus_snapshots(
    market: Literal["kr", "us"] = "kr",
    symbols: list[str] | None = None,
    limit: int | None = 20,
    all_symbols: bool = False,
    batch_size: int = 100,
    concurrency: int = 4,
    commit: bool = False,
) -> dict[str, Any]:
    """Build analyst_consensus_snapshots rows, dry-run by default."""
    result = await run_analyst_consensus_snapshot_build(
        AnalystConsensusSnapshotBuildRequest(
            market=market,
            symbols=tuple(symbols or ()),
            limit=limit,
            all_symbols=all_symbols,
            batch_size=batch_size,
            concurrency=concurrency,
            commit=commit,
        )
    )
    return {
        "market": result.market,
        "symbolsResolved": result.symbols_resolved,
        "snapshotsBuilt": result.snapshots_built,
        "committed": result.committed,
        "batches": result.batches,
        "startedAt": result.started_at.isoformat(),
        "finishedAt": result.finished_at.isoformat(),
        "snapshotDateDistribution": result.snapshot_date_distribution,
        "idempotency": result.idempotency,
        "samples": [
            {
                "market": sample.market,
                "symbol": sample.symbol,
                "source": sample.source,
                "snapshotDate": sample.snapshot_date.isoformat(),
                "totalCount": sample.total_count,
                "targetMean": str(sample.target_mean)
                if sample.target_mean is not None
                else None,
                "currentPrice": str(sample.current_price)
                if sample.current_price is not None
                else None,
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }
