"""investor_flow collector — 투자자 매매동향을 번들 evidence로 노출 (ROB-398 Slice 3).

read-only: InvestorFlowSnapshot을 query_service로 읽기만 한다. optional/non-blocking.
"""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.investor_flow_snapshots.query_service import InvestorFlowQueryService
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)


class InvestorFlowSnapshotCollector:
    """Optional ``investor_flow`` collector backed by investor_flow_snapshots."""

    snapshot_kind: str = "investor_flow"

    def __init__(
        self,
        session: AsyncSession | None,
        *,
        query_service: InvestorFlowQueryService | None = None,
    ) -> None:
        self._query = query_service or InvestorFlowQueryService(
            InvestorFlowSnapshotsRepository(session)
        )

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        if request.market != "kr":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"investor_flow unsupported for market={request.market}",
                    as_of=now,
                )
            ]
        symbols = request.symbols or []
        if not symbols:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="no_symbols_requested",
                    as_of=now,
                )
            ]
        try:
            flow = await self._query.get_investor_flow(
                symbols=symbols, market="kr", now=now
            )
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"investor_flow query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        overall = flow.freshness.overall
        if overall == "unavailable":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=flow.freshness.stale_reason or "unavailable",
                    as_of=now,
                )
            ]
        freshness_status = "fresh" if overall == "fresh" else "soft_stale"
        payload = {
            "market": "kr",
            "snapshot_date": (
                flow.snapshot_date.isoformat() if flow.snapshot_date else None
            ),
            "freshness": asdict(flow.freshness),
            "rows": [asdict(r) for r in flow.rows],
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                freshness_status=freshness_status,
            )
        ]
