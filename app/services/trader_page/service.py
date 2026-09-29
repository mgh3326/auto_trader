"""Read-only service behind the /trader operator page (task 889, stage 1).

Deliberately thin: open orders delegate to ``CurrentOrdersService`` (the
existing Toss/KIS/Upbit read path used by /invest), fills to
``ExecutionLedgerQueryService`` on ``review.execution_ledger``, and watches to
``InvestmentReportsRepository.list_active_alerts(valid_at=...)`` on
``review.investment_watch_alerts``. Nothing here may call an order, approval,
or watch-mutation surface — pinned by tests/test_trader_page_safety.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timezone import kst_day_window
from app.schemas.open_orders import OpenOrdersResponse
from app.schemas.trader_page import (
    TOSS_APP_ORDERS_NOTE,
    TOSS_FILL_POLLER_NOTE,
    TraderFillsResponse,
    TraderOpenOrdersCacheMeta,
    TraderOpenOrderSource,
    TraderOpenOrdersResponse,
    TraderWatchesResponse,
    TraderWatchRow,
)
from app.services.current_orders_service import CurrentOrdersService
from app.services.execution_ledger.query_service import ExecutionLedgerQueryService
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.trader_page.open_orders_cache import (
    OPEN_ORDERS_CACHE,
    TraderOpenOrdersCache,
)

logger = logging.getLogger(__name__)


class TraderPageService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        cache: TraderOpenOrdersCache | None = None,
        orders_service: CurrentOrdersService | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._db = db
        self._cache = cache if cache is not None else OPEN_ORDERS_CACHE
        self._orders = orders_service or CurrentOrdersService(db=db)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ttl = int(
            ttl_seconds
            if ttl_seconds is not None
            else settings.trader_open_orders_cache_ttl_seconds
        )

    async def open_orders(self, *, refresh: bool = False) -> TraderOpenOrdersResponse:
        now = self._clock()
        cached_at: datetime | None = None
        response: OpenOrdersResponse | None = None
        if not refresh:
            response, cached_at = await self._cache.read(now)
        hit = response is not None
        if response is None:
            # Capture the invalidation generation BEFORE the broker fan-out:
            # a fill committing mid-fetch then mismatches on the next read
            # instead of being stamped onto data that predates it.
            seen_gen = await self._cache.generation()
            response = await self._orders.list_open_orders(market="all")
            self._cache.record_last_ok(response)
            await self._cache.store(response, now, self._ttl, seen_gen=seen_gen)
            cached_at = now
        sources = [
            TraderOpenOrderSource(
                broker=source.broker,
                market=source.market,
                status=source.status,
                count=source.count,
                message=source.message,
                fetched_at=source.fetched_at,
                last_ok_at=self._cache.last_ok(source.broker, source.market),
            )
            for source in response.sources
        ]
        return TraderOpenOrdersResponse(
            as_of=response.as_of,
            data_state=response.data_state,
            count=response.count,
            items=response.items,
            sources=sources,
            cache=TraderOpenOrdersCacheMeta(
                ttl_seconds=self._ttl,
                cached_at=cached_at,
                hit=hit,
            ),
            warnings=list(response.warnings),
            notes=[TOSS_APP_ORDERS_NOTE],
            empty_reason=response.empty_reason,
        )

    async def fills_today(self) -> TraderFillsResponse:
        now = self._clock()
        window_start, window_end = kst_day_window(now)
        fills = await ExecutionLedgerQueryService(self._db).list_fills_today(now=now)
        return TraderFillsResponse(
            day_kst=window_start.strftime("%Y-%m-%d"),
            window_start=window_start,
            window_end=window_end,
            count=fills.count,
            items=fills.items,
            data_state=fills.data_state,
            empty_reason=fills.empty_reason,
            notes=[TOSS_FILL_POLLER_NOTE],
        )

    async def active_watches(self) -> TraderWatchesResponse:
        now = self._clock()
        alerts = await InvestmentReportsRepository(self._db).list_active_alerts(
            valid_at=now
        )
        rows = [
            TraderWatchRow(
                alert_uuid=alert.alert_uuid,
                market=alert.market,
                symbol=alert.symbol,
                intent=alert.intent,
                metric=alert.metric,
                operator=alert.operator,
                threshold=alert.threshold,
                threshold_high=alert.threshold_high,
                valid_until=alert.valid_until,
                action_mode=alert.action_mode,
                status=alert.status,
            )
            for alert in alerts
        ]
        rows.sort(key=lambda row: row.valid_until)
        return TraderWatchesResponse(as_of=now, count=len(rows), items=rows)
