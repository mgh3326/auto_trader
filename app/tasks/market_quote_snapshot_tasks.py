"""TaskIQ wrappers for quote snapshot freshness.

No recurring schedule is attached intentionally. Operators can enqueue this task
manually in dry-run mode to produce an approval packet. Persisting rows requires
commit=True and separate production database-write approval.
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.taskiq_broker import broker
from app.jobs.market_quote_snapshots import (
    MarketQuoteSnapshotBuildRequest,
    run_market_quote_snapshot_build,
)


@broker.task(task_name="build_market_quote_snapshots")
async def build_market_quote_snapshots(
    market: Literal["kr", "us"] = "kr",
    symbols: list[str] | None = None,
    limit: int | None = 20,
    all_symbols: bool = False,
    batch_size: int = 100,
    concurrency: int = 4,
    commit: bool = False,
) -> dict[str, Any]:
    """Build market_quote_snapshots rows, dry-run by default."""
    result = await run_market_quote_snapshot_build(
        MarketQuoteSnapshotBuildRequest(
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
        "snapshotAtDistribution": result.snapshot_at_distribution,
        "idempotency": result.idempotency,
        "samples": [
            {
                "market": sample.market,
                "symbol": sample.symbol,
                "source": sample.source,
                "snapshotAt": sample.snapshot_at.isoformat(),
                "price": str(sample.price),
                "previousClose": str(sample.previous_close)
                if sample.previous_close is not None
                else None,
                "volume": sample.volume,
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }
