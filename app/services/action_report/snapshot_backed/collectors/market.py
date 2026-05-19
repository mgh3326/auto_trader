"""Market-events snapshot collector (read-only).

Reads market events for a small window around today via
:class:`MarketEventsQueryService`. The query service is read-only by
design (ROB-128).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

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
from app.services.market_events.query_service import MarketEventsQueryService

_MARKET_TO_QUERY: dict[str, str | None] = {
    # Market events upstream keys: "kr" / "us" — there is no "crypto"
    # category at the moment so we surface only KR/US events when present
    # and an empty payload otherwise (still counts as fresh — no events is
    # a valid state).
    "kr": "kr",
    "us": "us",
    "crypto": None,
}


class MarketEventsSnapshotCollector:
    """Required-kind ``market`` collector backed by ``market_events``."""

    snapshot_kind: str = "market"

    def __init__(
        self,
        session: AsyncSession,
        *,
        query_service: MarketEventsQueryService | None = None,
        lookback_days: int = 0,
        lookahead_days: int = 1,
    ) -> None:
        self._session = session
        self._query = query_service or MarketEventsQueryService(session)
        self._lookback = max(0, lookback_days)
        self._lookahead = max(0, lookahead_days)

    async def collect(
        self, request: CollectorRequest
    ) -> list[SnapshotCollectResult]:
        now = utcnow()
        market_key = _MARKET_TO_QUERY.get(request.market)

        today = now.date()
        from_date = today - dt.timedelta(days=self._lookback)
        to_date = today + dt.timedelta(days=self._lookahead)

        try:
            response = await self._query.list_for_range(
                from_date=from_date,
                to_date=to_date,
                market=market_key,
            )
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"market_events query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        events_payload: list[dict[str, Any]] = [
            event.model_dump(mode="json") for event in response.events
        ]
        payload: dict[str, Any] = {
            "market": request.market,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "event_count": len(events_payload),
            "events": events_payload,
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={
                    "event_count": len(events_payload),
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                },
            )
        ]
