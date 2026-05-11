"""ROB-144 — calendar view-model assembler (uses MarketEventsQueryService)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_calendar import (
    Badge,
    CalendarCluster,
    CalendarDay,
    CalendarDaySummary,
    CalendarEvent,
    CalendarMarket,
    CalendarMeta,
    CalendarRelatedSymbol,
    CalendarResponse,
    CalendarTab,
    EventType,
    HighlightReason,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.market_events.freshness_service import MarketEventsFreshnessService
from app.services.market_events.query_service import MarketEventsQueryService

CLUSTER_THRESHOLD = 10
DAY_HIGHLIGHT_LIMIT = 5
CLUSTER_TOP_EVENT_LIMIT = 5

_EVENT_TYPE_ORDER: dict[EventType, int] = {
    "economic": 0,
    "earnings": 1,
    "disclosure": 2,
    "crypto": 3,
    "other": 4,
}
_MARKET_ORDER: dict[CalendarMarket, int] = {"kr": 0, "us": 1, "global": 2, "crypto": 3}
_HIGH_IMPACT_TERMS = ("cpi", "fomc", "nonfarm", "payroll", "pce", "gdp", "금리", "고용")

_MARKET_LITERAL = cast  # alias — used for type narrowing below


def _event_date_key(value: datetime | None) -> tuple[int, str]:
    if value is None:
        return (1, "")
    return (0, value.isoformat())


def _is_high_impact_macro(event: CalendarEvent) -> bool:
    if event.eventType != "economic":
        return False
    haystack = f"{event.title} {event.source}".lower()
    return any(term in haystack for term in _HIGH_IMPACT_TERMS)


def _rank_calendar_event(
    event: CalendarEvent,
    *,
    target_date: date,
    today: date | None = None,
) -> tuple[int, list[HighlightReason]]:
    score = 0
    reasons: list[HighlightReason] = []
    if event.relation in ("held", "both"):
        score += 1000
        reasons.append("held")
    if event.relation in ("watchlist", "both"):
        score += 700
        reasons.append("watchlist")
    if "major" in event.badges:
        score += 500
        reasons.append("major")
    if _is_high_impact_macro(event):
        score += 400
        reasons.append("high_impact")
    ref_today = today or date.today()
    if target_date in (ref_today, ref_today + timedelta(days=1)):
        score += 100
        reasons.append("near_term")
    if (
        event.actual is not None
        or event.forecast is not None
        or event.previous is not None
    ):
        score += 50
        reasons.append("has_values")
    return score, reasons


def _sort_key(
    event: CalendarEvent,
) -> tuple[int, int, tuple[int, str], int, int, str, str]:
    return (
        -event.displayPriority,
        _EVENT_TYPE_ORDER.get(event.eventType, 99),
        _event_date_key(event.eventTimeLocal),
        _MARKET_ORDER.get(event.market, 99),
        0 if event.title else 1,
        event.title.lower(),
        event.eventId,
    )


def _with_priority(event: CalendarEvent, *, target_date: date) -> CalendarEvent:
    score, reasons = _rank_calendar_event(event, target_date=target_date)
    return event.model_copy(
        update={"displayPriority": score, "highlightReasons": reasons}
    )


def _sort_calendar_events(events: list[CalendarEvent]) -> list[CalendarEvent]:
    return sorted(events, key=_sort_key)


def _build_day_summary(all_events: list[CalendarEvent]) -> CalendarDaySummary | None:
    total = len(all_events)
    if total == 0:
        return None
    highlights = _sort_calendar_events(all_events)[:DAY_HIGHLIGHT_LIMIT]
    highlight_ids = [ev.eventId for ev in highlights]
    overflow = max(total - len(highlight_ids), 0)
    overflow_label = f"그 외 {overflow}개" if overflow else None
    headline = f"주요 일정 {len(highlight_ids)}개"
    if overflow_label:
        headline = f"{headline} · {overflow_label}"
    return CalendarDaySummary(
        headline=headline,
        highlightEventIds=highlight_ids,
        overflowCount=overflow,
        overflowLabel=overflow_label,
    )


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
    freshness_svc = MarketEventsFreshnessService(db)

    range_resp = await svc.list_for_range(from_date, to_date)
    per_day_states = await freshness_svc.get_per_day_states(from_date, to_date)
    coverage_matrix = await freshness_svc.get_coverage_matrix(from_date, to_date)

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
        events = _sort_calendar_events(
            [_with_priority(ev, target_date=d) for ev in by_day.get(d, [])]
        )
        summary = _build_day_summary(events)
        clusters: list[CalendarCluster] = []
        if len(events) > CLUSTER_THRESHOLD:
            grouped: dict[tuple[EventType, CalendarMarket], list[CalendarEvent]] = {}
            for ev in events:
                grouped.setdefault((ev.eventType, ev.market), []).append(ev)
            kept: list[CalendarEvent] = []
            for (etype, market), group in sorted(
                grouped.items(),
                key=lambda item: (
                    _EVENT_TYPE_ORDER.get(item[0][0], 99),
                    _MARKET_ORDER.get(item[0][1], 99),
                ),
            ):
                group = _sort_calendar_events(group)
                if len(group) > CLUSTER_TOP_EVENT_LIMIT:
                    clusters.append(
                        CalendarCluster(
                            clusterId=f"{d.isoformat()}:{etype}:{market}",
                            label=f"{etype} {market}".strip(),
                            eventType=etype,
                            market=market,
                            eventCount=len(group),
                            topEvents=group[:CLUSTER_TOP_EVENT_LIMIT],
                        )
                    )
                else:
                    kept.extend(group)
            events = kept
        day_state = per_day_states.get(d, "missing")
        days.append(
            CalendarDay(
                date=d,
                events=events,
                clusters=clusters,
                dataState=day_state,
                summary=summary,
            )
        )

    meta = CalendarMeta(
        sourceFreshness=list(coverage_matrix.sources),
        coverage=coverage_matrix.coverage,
    )

    return CalendarResponse(
        tab=tab,
        fromDate=from_date,
        toDate=to_date,
        asOf=datetime.now(UTC),
        days=days,
        meta=meta,
    )


def _date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)
