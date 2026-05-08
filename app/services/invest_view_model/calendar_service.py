"""ROB-144 — calendar view-model assembler (uses MarketEventsQueryService)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_calendar import (
    Badge,
    CalendarCluster,
    CalendarDay,
    CalendarEvent,
    CalendarMarket,
    CalendarMeta,
    CalendarRelatedSymbol,
    CalendarResponse,
    CalendarTab,
    EventType,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.market_events.query_service import MarketEventsQueryService

CLUSTER_THRESHOLD = 10

_MARKET_LITERAL = cast  # alias — used for type narrowing below


def _normalize_event_type(value: str | None) -> EventType:
    v = (value or "").lower()
    if v in ("earnings", "economic", "disclosure", "crypto"):
        return cast(EventType, v)
    return "other"


def _normalize_market(value: str | None) -> CalendarMarket:
    v = (value or "").lower()
    if v in ("kr", "us", "crypto", "global"):
        return cast(CalendarMarket, v)
    return "global"


async def build_calendar(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    from_date: date,
    to_date: date,
    tab: CalendarTab,
) -> CalendarResponse:
    svc = MarketEventsQueryService(db)
    range_resp = await svc.list_for_range(from_date, to_date)

    by_day: dict[date, list[CalendarEvent]] = {}
    # range_resp.events is a list[MarketEventResponse]
    for raw in getattr(range_resp, "events", []):
        market = _normalize_market(getattr(raw, "market", None))
        # MarketEventResponse uses 'category', not 'event_type'
        etype = _normalize_event_type(getattr(raw, "category", None))
        if tab != "all" and etype != tab:
            continue
        symbol = getattr(raw, "symbol", None)
        related: list[CalendarRelatedSymbol] = []
        relation = "none"
        if symbol and market in ("kr", "us", "crypto"):
            # MarketEventResponse has 'company_name', not 'symbol_display_name'
            display_name = str(getattr(raw, "company_name", None) or symbol)
            related.append(
                CalendarRelatedSymbol(
                    symbol=str(symbol),
                    market=market,  # type: ignore[arg-type]
                    displayName=display_name,
                )
            )
            relation = resolver.relation(market, symbol)

        badges: list[Badge] = []
        if relation in ("held", "both"):
            badges.append("holdings")
        if relation in ("watchlist", "both"):
            badges.append("watchlist")

        # MarketEventResponse has 'source_event_id' (no bare 'event_id')
        event_id = str(
            getattr(raw, "source_event_id", None) or getattr(raw, "id", None) or ""
        )

        # actual/forecast/previous live in values[], not at top level
        raw_values = getattr(raw, "values", None) or []
        first_val = raw_values[0] if raw_values else None
        actual = (
            str(getattr(first_val, "actual", None))
            if first_val and getattr(first_val, "actual", None) is not None
            else None
        )
        forecast = (
            str(getattr(first_val, "forecast", None))
            if first_val and getattr(first_val, "forecast", None) is not None
            else None
        )
        previous = (
            str(getattr(first_val, "previous", None))
            if first_val and getattr(first_val, "previous", None) is not None
            else None
        )

        # MarketEventResponse has 'release_time_utc', not 'event_time_local'
        event_time = getattr(raw, "release_time_utc", None)

        ev = CalendarEvent(
            eventId=event_id,
            title=str(getattr(raw, "title", "") or ""),
            market=market,
            eventType=etype,
            eventTimeLocal=event_time,
            source=str(getattr(raw, "source", "") or ""),
            actual=actual,
            forecast=forecast,
            previous=previous,
            relatedSymbols=related,
            relation=relation,  # type: ignore[arg-type]
            badges=badges,
        )
        ev_date = getattr(raw, "event_date", None) or (
            ev.eventTimeLocal.date() if ev.eventTimeLocal else from_date
        )
        by_day.setdefault(ev_date, []).append(ev)

    days: list[CalendarDay] = []
    for d in _date_range(from_date, to_date):
        events = by_day.get(d, [])
        clusters: list[CalendarCluster] = []
        if len(events) > CLUSTER_THRESHOLD:
            grouped: dict[tuple[EventType, CalendarMarket], list[CalendarEvent]] = {}
            for ev in events:
                grouped.setdefault((ev.eventType, ev.market), []).append(ev)
            kept: list[CalendarEvent] = []
            for (etype, market), group in grouped.items():
                if len(group) > 5:
                    clusters.append(
                        CalendarCluster(
                            clusterId=f"{d.isoformat()}:{etype}:{market}",
                            label=f"{etype} {market}".strip(),
                            eventType=etype,
                            market=market,
                            eventCount=len(group),
                            topEvents=group[:5],
                        )
                    )
                else:
                    kept.extend(group)
            events = kept
        days.append(CalendarDay(date=d, events=events, clusters=clusters))

    return CalendarResponse(
        tab=tab,
        fromDate=from_date,
        toDate=to_date,
        asOf=datetime.now(UTC),
        days=days,
        meta=CalendarMeta(),
    )


def _date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)
