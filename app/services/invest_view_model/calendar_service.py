"""ROB-144 — calendar view-model assembler (uses MarketEventsQueryService)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

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
    ImpactTag,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.market_events.freshness_service import MarketEventsFreshnessService
from app.services.market_events.query_service import MarketEventsQueryService

CLUSTER_THRESHOLD = 10
DAY_HIGHLIGHT_LIMIT = 5
CLUSTER_TOP_EVENT_LIMIT = 5
KST = ZoneInfo("Asia/Seoul")

_EVENT_TYPE_ORDER: dict[EventType, int] = {
    "economic": 0,
    "earnings": 1,
    "disclosure": 2,
    "crypto": 3,
    "other": 4,
}
_MARKET_ORDER: dict[CalendarMarket, int] = {"kr": 0, "us": 1, "global": 2, "crypto": 3}
_HIGH_IMPACT_TERMS = (
    "cpi",
    "core cpi",
    "ppi",
    "core ppi",
    "producer price",
    "producer prices",
    "fomc",
    "fed interest rate",
    "interest rate decision",
    "nonfarm",
    "payroll",
    "unemployment rate",
    "jobless claims",
    "pce",
    "gdp",
    "retail sales",
    "ism",
    "금리",
    "고용",
    "생산자물가",
    "소비자물가",
)
_FX_TERMS = (
    "fx",
    "forex",
    "exchange rate",
    "dollar",
    "usd",
    "usd/krw",
    "usdkrw",
    "원달러",
    "달러원",
    "환율",
    "외환",
    "yen",
    "엔화",
    "eur",
    "jpy",
    "dxy",
)
_RATES_TERMS = (
    "rate",
    "rates",
    "yield",
    "treasury",
    "bond",
    "fomc",
    "boj",
    "ecb",
    "bok",
    "금리",
    "국채",
    "채권",
    "기준금리",
    "한국은행",
    "연준",
)
_INFLATION_TERMS = ("cpi", "ppi", "pce", "inflation", "price", "물가")
_JOBS_TERMS = ("nonfarm", "payroll", "unemployment", "jobless", "employment", "고용", "실업")
_CENTRAL_BANK_TERMS = ("fomc", "fed", "boj", "ecb", "bok", "central bank", "연준", "한국은행", "중앙은행")

_MARKET_LITERAL = cast  # alias — used for type narrowing below


def _event_date_key(value: object | None) -> tuple[int, str]:
    if value is None:
        return (1, "")
    if isinstance(value, datetime):
        return (0, value.isoformat())
    return (0, str(value))


def _is_high_impact_macro(event: CalendarEvent) -> bool:
    if event.eventType != "economic":
        return False
    if event.importance == 3:
        return True
    haystack = f"{event.title} {event.source} {event.currency or ''} {event.country or ''}".lower()
    return any(term in haystack for term in _HIGH_IMPACT_TERMS)


def _contains_any(haystack: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in haystack for term in terms)


def _impact_tags_for_macro(
    *, title: str, source: str, currency: str | None, country: str | None
) -> list[ImpactTag]:
    haystack = f"{title} {source} {currency or ''} {country or ''}".lower()
    tags: list[ImpactTag] = []
    if (currency or "").upper() in {"USD", "KRW", "JPY", "CNY", "EUR", "GBP"} or _contains_any(haystack, _FX_TERMS):
        tags.append("fx")
    if _contains_any(haystack, _RATES_TERMS):
        tags.append("rates")
    if _contains_any(haystack, _INFLATION_TERMS):
        tags.append("inflation")
    if _contains_any(haystack, _JOBS_TERMS):
        tags.append("jobs")
    if _contains_any(haystack, _CENTRAL_BANK_TERMS):
        tags.append("central_bank")
    return tags


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


def _format_calendar_value(value: object) -> str | None:
    if value is None:
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        text = str(value).strip()
        return text or None
    if decimal_value.is_zero():
        return "0"
    normalized = decimal_value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _format_kst_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    kst = aware.astimezone(KST)
    hour = kst.hour
    minute = kst.minute
    period = "오전" if hour < 12 else "오후"
    hour12 = hour % 12 or 12
    minute_text = f" {minute}분" if minute else ""
    return f"{kst.month}월 {kst.day}일 {period} {hour12}시{minute_text} KST"


def _event_title(raw: object, *, market: CalendarMarket, etype: EventType) -> str:
    title = str(getattr(raw, "title", "") or "").strip()
    if title:
        return title
    company_name = str(getattr(raw, "company_name", "") or "").strip()
    symbol = str(getattr(raw, "symbol", "") or "").strip()
    if company_name and symbol and company_name != symbol:
        entity = f"{company_name}({symbol})"
    else:
        entity = company_name or symbol
    if entity and etype == "earnings":
        return f"{entity} 실적 발표"
    if entity:
        return entity
    if etype == "earnings" and market == "kr":
        return "국내 기업 실적 발표"
    if etype == "earnings" and market == "us":
        return "미국 기업 실적 발표"
    if etype == "economic":
        return "경제지표 발표"
    return "시장 이벤트"


def _source_priority(source: str | None) -> int:
    """Provider preference for duplicate /invest calendar rows.

    TradingView economic-calendar rows currently carry cleaner actual/forecast/
    previous values than ForexFactory for the same macro release.  ForexFactory
    remains useful as fallback and for rows TradingView does not provide.
    """
    if (source or "").lower() == "tradingview":
        return 20
    if (source or "").lower() == "forexfactory":
        return 10
    return 0


def _normalize_economic_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _economic_dedupe_key(
    ev: CalendarEvent, ev_date: date
) -> tuple[str, date, str] | None:
    """Return a narrow duplicate key for global macro events only."""
    if ev.eventType != "economic" or ev.market != "global":
        return None
    normalized_title = _normalize_economic_title(ev.title)
    if not normalized_title:
        return None
    return ("economic", ev_date, normalized_title)


def _dedupe_calendar_events(
    events: list[CalendarEvent], ev_date: date
) -> list[CalendarEvent]:
    """Deduplicate same-day economic/global provider duplicates.

    The de-duplication is intentionally applied after conversion to the invest
    calendar DTO so provider fallback is preserved: if TradingView is missing,
    the ForexFactory row remains; if both are present for the same release,
    TradingView wins regardless of query order.
    """
    deduped: list[CalendarEvent] = []
    key_to_index: dict[tuple[str, date, str], int] = {}
    for ev in events:
        key = _economic_dedupe_key(ev, ev_date)
        if key is None:
            deduped.append(ev)
            continue
        existing_idx = key_to_index.get(key)
        if existing_idx is None:
            key_to_index[key] = len(deduped)
            deduped.append(ev)
            continue
        existing = deduped[existing_idx]
        if _source_priority(ev.source) > _source_priority(existing.source):
            deduped[existing_idx] = ev
    return deduped


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
        actual = _format_calendar_value(getattr(first_val, "actual", None))
        forecast = _format_calendar_value(getattr(first_val, "forecast", None))
        previous = _format_calendar_value(getattr(first_val, "previous", None))

        # MarketEventResponse has 'release_time_utc', not 'event_time_local'
        event_time = getattr(raw, "release_time_utc", None)
        ev_date = getattr(raw, "event_date", None) or (
            event_time.date() if event_time else from_date
        )

        title = _event_title(raw, market=market, etype=etype)
        source = str(getattr(raw, "source", "") or "")
        currency = getattr(raw, "currency", None)
        country = getattr(raw, "country", None)
        ev = CalendarEvent(
            eventId=event_id,
            title=title,
            market=market,
            eventType=etype,
            eventTimeLocal=_format_kst_time(event_time),
            source=source,
            country=country,
            currency=currency,
            importance=getattr(raw, "importance", None),
            impactTags=_impact_tags_for_macro(
                title=title,
                source=source,
                currency=currency,
                country=country,
            )
            if etype == "economic"
            else [],
            actual=actual,
            forecast=forecast,
            previous=previous,
            relatedSymbols=related,
            relation=relation,  # type: ignore[arg-type]
            badges=badges,
        )
        by_day.setdefault(ev_date, []).append(ev)

    days: list[CalendarDay] = []
    for d in _date_range(from_date, to_date):
        events = _dedupe_calendar_events(by_day.get(d, []), d)
        events = _sort_calendar_events(
            [_with_priority(ev, target_date=d) for ev in events]
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
