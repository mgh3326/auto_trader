"""TaskIQ wrappers for KR investor-flow snapshot freshness.

No recurring schedule is attached intentionally. Operators can enqueue this task
manually in dry-run mode to produce an approval packet. Persisting rows requires
commit=True and separate production database-write approval.
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.taskiq_broker import broker
from app.jobs.investor_flow_snapshots import (
    InvestorFlowSnapshotBuildRequest,
    run_investor_flow_snapshot_build,
)


@broker.task(task_name="build_investor_flow_snapshots")
async def build_investor_flow_snapshots(
    market: Literal["kr"] = "kr",
    symbols: list[str] | None = None,
    limit: int | None = 20,
    all_symbols: bool = False,
    batch_size: int = 100,
    concurrency: int = 4,
    days: int = 20,
    commit: bool = False,
) -> dict[str, Any]:
    """Build KR investor_flow_snapshots rows, dry-run by default."""
    request = InvestorFlowSnapshotBuildRequest(
        market=market,
        symbols=tuple(symbols or ()),
        limit=limit,
        all_symbols=all_symbols,
        batch_size=batch_size,
        concurrency=concurrency,
        days=days,
        commit=commit,
    )
    result = await run_investor_flow_snapshot_build(request)
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
                "snapshotDate": sample.snapshot_date.isoformat(),
                "source": sample.source,
                "foreignNet": sample.foreign_net,
                "institutionNet": sample.institution_net,
                "individualNet": sample.individual_net,
                "doubleBuy": sample.double_buy,
                "doubleSell": sample.double_sell,
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }
