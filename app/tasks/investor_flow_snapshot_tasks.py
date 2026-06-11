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

#: ROB-512 갭4: Naver frgn(일별 수급 확정 행)은 당일 저녁엔 부분 발행이라
#: (2026-06-10 18:10 KST 실측 144/3,909 종목 → thin 파티션 → older_fallback)
#: 당일 cron(구 ROB-438 "40 16")은 구조적으로 당일 데이터를 못 잡는다. 행은
#: 익일 아침에 완성되므로 개장 전 08:30 KST에 전 거래일(D-1)을 적재한다.
#: 휴장일 결행분은 빌더의 days=20 히스토리 upsert가 다음 run에서 백필한다.
_KR_FLOW_CRON = "30 8 * * 1-5"


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
    schedule=_kr_flow_schedule(_KR_FLOW_CRON),
)
async def scheduled_kr_investor_flow() -> dict[str, Any]:
    """ROB-438/ROB-512: KR investor-flow refresh at 08:30 KST (Naver 수급, 익일 아침).

    전 거래일(D-1) 확정 수급을 적재한다 — 당일 행은 저녁까지 부분 발행이라 당일
    cron은 thin 파티션만 만든다(ROB-512 갭4 실측, ``_KR_FLOW_CRON`` 주석 참조).
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
