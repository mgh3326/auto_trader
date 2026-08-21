"""Narrow calendar event projection: no raw payload, one values batch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent, MarketEventValue

_EVENT_COLUMNS = (
    MarketEvent.id,
    MarketEvent.category,
    MarketEvent.market,
    MarketEvent.country,
    MarketEvent.currency,
    MarketEvent.symbol,
    MarketEvent.company_name,
    MarketEvent.title,
    MarketEvent.event_date,
    MarketEvent.release_time_utc,
    MarketEvent.importance,
    MarketEvent.source,
    MarketEvent.source_event_id,
)


@dataclass(frozen=True, slots=True)
class CalendarValueProjection:
    actual: Any
    forecast: Any
    previous: Any


@dataclass(frozen=True, slots=True)
class CalendarEventProjection:
    id: int
    category: str
    market: str
    country: str | None
    currency: str | None
    symbol: str | None
    company_name: str | None
    title: str | None
    event_date: date
    release_time_utc: datetime | None
    importance: int | None
    source: str
    source_event_id: str | None
    values: tuple[CalendarValueProjection, ...]


async def load_calendar_events(
    db: AsyncSession,
    from_date: date,
    to_date: date,
) -> list[CalendarEventProjection]:
    """Load range events once and all values in one query.

    Event order matches the generic query service (``event_date``, ``symbol``).
    Values are ordered by primary key so the first value is the first inserted
    row, matching the previous per-event select.
    """
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")

    event_rows = (
        await db.execute(
            select(*_EVENT_COLUMNS)
            .where(
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
            .order_by(MarketEvent.event_date.asc(), MarketEvent.symbol.asc())
        )
    ).all()
    if not event_rows:
        return []

    event_ids = [row.id for row in event_rows]
    value_rows = (
        await db.execute(
            select(
                MarketEventValue.event_id,
                MarketEventValue.id,
                MarketEventValue.actual,
                MarketEventValue.forecast,
                MarketEventValue.previous,
            )
            .where(MarketEventValue.event_id.in_(event_ids))
            .order_by(
                MarketEventValue.event_id.asc(),
                MarketEventValue.id.asc(),
            )
        )
    ).all()
    values_by_event: dict[int, list[CalendarValueProjection]] = {
        event_id: [] for event_id in event_ids
    }
    for value in value_rows:
        values_by_event[value.event_id].append(
            CalendarValueProjection(
                actual=value.actual,
                forecast=value.forecast,
                previous=value.previous,
            )
        )

    return [
        CalendarEventProjection(
            id=row.id,
            category=row.category,
            market=row.market,
            country=row.country,
            currency=row.currency,
            symbol=row.symbol,
            company_name=row.company_name,
            title=row.title,
            event_date=row.event_date,
            release_time_utc=row.release_time_utc,
            importance=row.importance,
            source=row.source,
            source_event_id=row.source_event_id,
            values=tuple(values_by_event.get(row.id, ())),
        )
        for row in event_rows
    ]
