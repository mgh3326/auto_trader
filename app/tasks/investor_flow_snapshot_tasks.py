"""TaskIQ wrappers for KR investor-flow snapshot freshness.

Manual entry (``build_investor_flow_snapshots``, dry-run by default) plus the
ROB-438 default-off scheduled wrapper. Recurring activation is double-gated like
invest_screener (ROB-281): ``investor_flow_schedule_enabled`` registers the cron
(off → manual ``taskiq kick`` only), ``investor_flow_snapshots_commit_enabled``
allows DB writes (off → dry-run-on-cron). Operator flips both to activate.
"""

from __future__ import annotations

from typing import Any, Literal

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.investor_flow_snapshots import (
    InvestorFlowSnapshotBuildRequest,
    run_investor_flow_snapshot_build,
)

_KST_LABEL = "Asia/Seoul"


def _kr_flow_schedule(cron: str) -> list[dict[str, str]]:
    """Cron labels gated by the schedule flag (default off → [] → not registered)."""
    if not settings.investor_flow_schedule_enabled:
        return []
    return [{"cron": cron, "cron_offset": _KST_LABEL}]


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


@broker.task(
    task_name="investor_flow_snapshots.kr_scheduled",
    schedule=_kr_flow_schedule("40 16 * * 1-5"),
)
async def scheduled_kr_investor_flow() -> dict[str, Any]:
    """ROB-438: KR investor-flow refresh at 16:40 KST (Naver 수급, post-close).

    Holiday-gated via XKRX (skips non-trading days). Default-off; commit gated by
    ``investor_flow_snapshots_commit_enabled`` (dry-run-on-cron until operator sets it).
    """
    from app.tasks.invest_screener_snapshot_tasks import is_market_session_today

    if not is_market_session_today("kr"):
        return {"status": "skipped_holiday", "market": "kr"}
    return await build_investor_flow_snapshots(
        market="kr",
        all_symbols=True,
        commit=settings.investor_flow_snapshots_commit_enabled,
    )
