"""Portfolio snapshot collector (read-only).

Reads the user's currently-known holdings from the local DB state
(``manual_holdings``). The upstream broker-sync flows
(`PortfolioDataCollector`, broker websocket reconcilers, etc.) populate
that table — this collector never calls a broker itself, so it is
inherently safe to invoke during automated report generation.

Mapping to the report ticket's account_scope:

* ``account_scope='kis_live'`` → KR + US ``manual_holdings`` rows.
* ``account_scope='upbit_live'`` → CRYPTO ``manual_holdings`` rows.

If the request's market doesn't match a configured scope, the collector
returns ``unavailable`` rather than raising — the bundle service then
records it as a required-kind gap.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import ManualHolding, MarketType
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_MARKET_TO_TYPES: dict[str, tuple[MarketType, ...]] = {
    "kr": (MarketType.KR,),
    "us": (MarketType.US,),
    "crypto": (MarketType.CRYPTO,),
}


class PortfolioSnapshotCollector:
    """Required-kind ``portfolio`` collector backed by ``manual_holdings``."""

    snapshot_kind: str = "portfolio"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def collect(
        self, request: CollectorRequest
    ) -> list[SnapshotCollectResult]:
        market_types = _MARKET_TO_TYPES.get(request.market)
        now = utcnow()
        if not market_types:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"no portfolio mapping for market={request.market!r}",
                    as_of=now,
                )
            ]

        stmt = select(ManualHolding).where(
            ManualHolding.market_type.in_(market_types)
        )
        rows = (await self._session.execute(stmt)).scalars().all()

        holdings: list[dict[str, Any]] = [
            {
                "ticker": row.ticker,
                "market_type": row.market_type.value
                if isinstance(row.market_type, MarketType)
                else str(row.market_type),
                "quantity": row.quantity,
                "avg_price": row.avg_price,
                "display_name": row.display_name,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

        payload = {
            "holdings": holdings,
            "count": len(holdings),
            "market": request.market,
        }

        if not holdings:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"holdings_found": False},
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={
                    "holdings_count": len(holdings),
                },
            )
        ]
