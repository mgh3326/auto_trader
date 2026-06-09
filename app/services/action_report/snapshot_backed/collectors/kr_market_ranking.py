"""kr_market_ranking collector — Naver 모멘텀 랭킹을 번들 evidence 로 노출 (ROB-398 Slice 2).

read-only: 모멘텀 스냅샷을 query_service 로 읽기만 한다. optional/non-blocking.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.invest_momentum_events.query_service import (
    MomentumRankingQueryService,
)
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_DEFAULT_ORDER_TYPES: tuple[str, ...] = ("up", "quantTop")  # 상승 + 거래량
_RANKING_LIMIT = 30


class KrMarketRankingSnapshotCollector:
    """Optional ``kr_market_ranking`` collector backed by momentum snapshots."""

    snapshot_kind: str = "kr_market_ranking"

    def __init__(
        self,
        session: AsyncSession | None,
        *,
        query_service: MomentumRankingQueryService | None = None,
        order_types: tuple[str, ...] = _DEFAULT_ORDER_TYPES,
    ) -> None:
        self._query = query_service or MomentumRankingQueryService(
            InvestMomentumEventSnapshotsRepository(session)
        )
        self._order_types = order_types

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        if request.market != "kr":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"kr_market_ranking unsupported for market={request.market}",
                    as_of=now,
                )
            ]
        try:
            order_payloads: dict[str, Any] = {}
            statuses: list[str] = []
            for order_type in self._order_types:
                ranking = await self._query.get_ranking(
                    order_type=order_type, market="kr", limit=_RANKING_LIMIT, now=now
                )
                order_payloads[order_type] = {
                    "trading_date": (
                        ranking.trading_date.isoformat()
                        if ranking.trading_date
                        else None
                    ),
                    "freshness": asdict(ranking.freshness),
                    "rows": [asdict(r) for r in ranking.rows],
                }
                statuses.append(ranking.freshness.overall)
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"momentum ranking query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        # overall: 하나라도 fresh면 fresh, 전부 unavailable이면 unavailable, 그 외 soft_stale.
        if any(s == "fresh" for s in statuses):
            freshness_status = "fresh"
        elif statuses and all(s == "unavailable" for s in statuses):
            freshness_status = "unavailable"
        else:
            freshness_status = "soft_stale"

        if freshness_status == "unavailable":
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="no_ranking_rows",
                    as_of=now,
                )
            ]

        payload = {"market": "kr", "order_types": order_payloads}
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
