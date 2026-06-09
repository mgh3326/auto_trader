"""TaskIQ wrappers for valuation snapshot freshness.

Manual entry (``build_market_valuation_snapshots``, dry-run by default) plus the
ROB-438 default-off scheduled wrapper. Recurring activation is double-gated like
invest_screener (ROB-281): ``market_valuation_schedule_enabled`` registers the cron
(off → manual ``taskiq kick`` only), ``market_valuation_snapshots_commit_enabled``
allows DB writes (off → dry-run-on-cron). Operator flips both to activate.
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.market_valuation_snapshots import (
    MarketValuationSnapshotBuildRequest,
    run_market_valuation_snapshot_build,
)

_KST_LABEL = "Asia/Seoul"


def _kr_valuation_schedule(cron: str) -> list[dict[str, str]]:
    """Cron labels gated by the schedule flag (default off → [] → not registered)."""
    if not settings.market_valuation_schedule_enabled:
        return []
    return [{"cron": cron, "cron_offset": _KST_LABEL}]


@broker.task(task_name="build_market_valuation_snapshots")
async def build_market_valuation_snapshots(
    market: Literal["kr", "us"] = "kr",
    symbols: list[str] | None = None,
    limit: int | None = 20,
    all_symbols: bool = False,
    batch_size: int = 100,
    concurrency: int = 4,
    commit: bool = False,
) -> dict[str, Any]:
    """Build market_valuation_snapshots rows, dry-run by default."""
    result = await run_market_valuation_snapshot_build(
        MarketValuationSnapshotBuildRequest(
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
                "per": str(sample.per) if sample.per is not None else None,
                "pbr": str(sample.pbr) if sample.pbr is not None else None,
                "roe": str(sample.roe) if sample.roe is not None else None,
                "dividendYield": str(sample.dividend_yield)
                if sample.dividend_yield is not None
                else None,
            }
            for sample in result.samples
        ],
        "warnings": list(result.warnings),
    }


@broker.task(
    task_name="market_valuation_snapshots.kr_scheduled",
    schedule=_kr_valuation_schedule("30 16 * * 1-5"),
)
async def scheduled_kr_market_valuation() -> dict[str, Any]:
    """ROB-438: KR valuation refresh at 16:30 KST (after KRX preliminary 16:20).

    Holiday-gated via XKRX (skips non-trading days). Default-off; commit gated by
    ``market_valuation_snapshots_commit_enabled`` (dry-run-on-cron until operator sets it).
    """
    from app.tasks.invest_screener_snapshot_tasks import is_market_session_today

    if not is_market_session_today("kr"):
        return {"status": "skipped_holiday", "market": "kr"}
    return await build_market_valuation_snapshots(
        market="kr",
        all_symbols=True,
        commit=settings.market_valuation_snapshots_commit_enabled,
    )
