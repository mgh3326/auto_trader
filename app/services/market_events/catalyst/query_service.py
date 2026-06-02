# app/services/market_events/catalyst/query_service.py
"""catalyst read-model query_service (ROB-408 Slice 1).

기존 market_events 위 read-only. catalyst 카테고리 + event_date 범위 행을 읽어
days_until·polarity·freshness 를 부착. raw_payload_json 접근 위해 ORM 직접 read
(MarketEventResponse는 raw_payload 미포함). reader DI로 DB-free 테스트.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent
from app.services.market_events.catalyst.contract import (
    CatalystEvent,
    Freshness,
    UpcomingCatalysts,
)
from app.services.market_events.catalyst.polarity import (
    CATALYST_CATEGORIES,
    resolve_polarity,
)

_KST = ZoneInfo("Asia/Seoul")
ReaderFn = Callable[..., Awaitable[Sequence[object]]]


def _orm_reader(session: AsyncSession) -> ReaderFn:
    async def reader(*, categories, from_date, to_date, market, symbols):
        stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.market == market,
                MarketEvent.category.in_(categories),
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
            .order_by(MarketEvent.event_date.asc(), MarketEvent.symbol.asc())
        )
        if symbols:
            stmt = stmt.where(MarketEvent.symbol.in_(list(symbols)))
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    return reader


def _to_event(row: object, *, now_date: dt.date) -> CatalystEvent:
    return CatalystEvent(
        symbol=getattr(row, "symbol", None),
        category=getattr(row, "category", "unknown"),
        title=getattr(row, "title", None),
        event_date=getattr(row, "event_date", now_date),
        days_until=(getattr(row, "event_date", now_date) - now_date).days,
        polarity=resolve_polarity(getattr(row, "category", "unknown"), getattr(row, "raw_payload_json", None)),
        source=getattr(row, "source", None),
    )


class CatalystQueryService:
    def __init__(self, session: AsyncSession | None, *, reader: ReaderFn | None = None):
        if reader is None and session is None:
            raise ValueError("session or reader required")
        self._reader = reader or _orm_reader(session)  # type: ignore[arg-type]

    async def get_upcoming_catalysts(
        self,
        *,
        symbols: Iterable[str] | None = None,
        market: str = "kr",
        within_days: int = 7,
        now: dt.datetime,
    ) -> UpcomingCatalysts:
        now_date = now.astimezone(_KST).date() if now.tzinfo else now.date()
        from_date = now_date
        to_date = now_date + dt.timedelta(days=within_days)
        symbols_list = list(symbols) if symbols is not None else None
        rows = await self._reader(
            categories=CATALYST_CATEGORIES,
            from_date=from_date,
            to_date=to_date,
            market=market,
            symbols=symbols_list,
        )
        events = tuple(_to_event(r, now_date=now_date) for r in rows)
        if not events:
            freshness = Freshness("unavailable", "no_upcoming_catalysts")
        else:
            freshness = Freshness("fresh", None)
        return UpcomingCatalysts(
            market=market,
            within_days=within_days,
            rows=events,
            freshness=freshness,
        )
