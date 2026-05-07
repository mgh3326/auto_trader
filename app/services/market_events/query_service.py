"""Read-only query service for market events (ROB-128).

NOTE: held / watched flags currently return None. Joining holdings / watchlist is
deferred to a follow-up — see docs/runbooks/market-events-ingestion.md.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent, MarketEventValue
from app.schemas.market_events import (
    MarketEventResponse,
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
    MarketEventValueResponse,
)
from app.services.market_events.taxonomy import (
    validate_category,
    validate_market,
)


class MarketEventsQueryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_date(
        self,
        target_date: date,
        *,
        category: str | None = None,
        market: str | None = None,
        source: str | None = None,
    ) -> MarketEventsDayResponse:
        events = await self._query(
            from_date=target_date,
            to_date=target_date,
            category=category,
            market=market,
            source=source,
        )
        return MarketEventsDayResponse(date=target_date, events=events)

    async def list_for_range(
        self,
        from_date: date,
        to_date: date,
        *,
        category: str | None = None,
        market: str | None = None,
        source: str | None = None,
    ) -> MarketEventsRangeResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        events = await self._query(
            from_date=from_date,
            to_date=to_date,
            category=category,
            market=market,
            source=source,
        )
        return MarketEventsRangeResponse(
            from_date=from_date,
            to_date=to_date,
            count=len(events),
            events=events,
        )

    async def _query(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str | None,
        market: str | None,
        source: str | None,
    ) -> list[MarketEventResponse]:
        if category is not None:
            validate_category(category)
        if market is not None:
            validate_market(market)

        stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
            .order_by(MarketEvent.event_date.asc(), MarketEvent.symbol.asc())
        )
        if category is not None:
            stmt = stmt.where(MarketEvent.category == category)
        if market is not None:
            stmt = stmt.where(MarketEvent.market == market)
        if source is not None:
            stmt = stmt.where(MarketEvent.source == source)

        rows = (await self.db.execute(stmt)).scalars().all()

        out: list[MarketEventResponse] = []
        for row in rows:
            value_rows = (
                (
                    await self.db.execute(
                        select(MarketEventValue).where(
                            MarketEventValue.event_id == row.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            out.append(
                MarketEventResponse(
                    category=row.category,
                    market=row.market,
                    country=row.country,
                    currency=row.currency,
                    symbol=row.symbol,
                    company_name=row.company_name,
                    title=row.title,
                    event_date=row.event_date,
                    release_time_utc=row.release_time_utc,
                    time_hint=row.time_hint,
                    importance=row.importance,
                    status=row.status,
                    source=row.source,
                    source_event_id=row.source_event_id,
                    source_url=row.source_url,
                    fiscal_year=row.fiscal_year,
                    fiscal_quarter=row.fiscal_quarter,
                    held=None,
                    watched=None,
                    values=[
                        MarketEventValueResponse.model_validate(v) for v in value_rows
                    ],
                )
            )
        return out
