"""Discover calendar service: groups market events by day for the Toss-style UI (ROB-138)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.schemas.market_events import MarketEventResponse
from app.schemas.market_events_calendar import (
    DiscoverCalendarDay,
    DiscoverCalendarEvent,
    DiscoverCalendarResponse,
)
from app.services.market_events.prioritization import Priority, compute_priority
from app.services.market_events.query_service import MarketEventsQueryService
from app.services.market_events.user_context import UserEventContext

PER_DAY_VISIBLE_LIMIT = 8

Tab = Literal["all", "economic", "earnings"]

KO_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

PRIORITY_LABEL = {
    Priority.HELD: "held",
    Priority.WATCHED: "watched",
    Priority.MAJOR: "major",
    Priority.HIGH_IMPORTANCE: "high",
    Priority.MEDIUM_IMPORTANCE: "medium",
    Priority.OTHER: "other",
}

PRIORITY_BADGE = {
    Priority.HELD: "보유",
    Priority.WATCHED: "관심",
    Priority.MAJOR: "주요",
    Priority.HIGH_IMPORTANCE: None,
    Priority.MEDIUM_IMPORTANCE: None,
    Priority.OTHER: None,
}

TIME_HINT_LABEL = {
    "before_market": "장 전",
    "open": "장 중",
    "after_market": "장 마감 후",
    "unknown": None,
}
KST = ZoneInfo("Asia/Seoul")


def _format_korean_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    kst = dt.astimezone(KST)
    meridiem = "오전" if kst.hour < 12 else "오후"
    hour = kst.hour % 12 or 12
    if kst.minute:
        return f"{meridiem} {hour}시 {kst.minute}분"
    return f"{meridiem} {hour}시"


def _format_time_label(event: MarketEventResponse) -> str | None:
    if event.release_time_utc is not None:
        return _format_korean_time(event.release_time_utc)
    if event.time_hint and event.time_hint in TIME_HINT_LABEL:
        return TIME_HINT_LABEL[event.time_hint]
    return None


def _event_sort_time(event: MarketEventResponse) -> datetime:
    if event.release_time_utc is not None:
        if event.release_time_utc.tzinfo is None:
            return event.release_time_utc.replace(tzinfo=UTC)
        return event.release_time_utc
    return datetime.combine(event.event_date, time.min, tzinfo=UTC)


def _format_subtitle(event: MarketEventResponse) -> str | None:
    if event.category == "earnings":
        eps = next((v for v in event.values if v.metric_name == "eps"), None)
        if eps and (eps.actual is not None or eps.forecast is not None):
            actual = "-" if eps.actual is None else str(eps.actual)
            forecast = "-" if eps.forecast is None else str(eps.forecast)
            return f"EPS {actual} · 예측 {forecast}"
        return None
    if event.category == "economic":
        actual = next((v for v in event.values if v.metric_name == "actual"), None)
        if actual is None:
            return None
        unit = actual.unit or ""
        a = "-" if actual.actual is None else str(actual.actual)
        f = "-" if actual.forecast is None else str(actual.forecast)
        p = "-" if actual.previous is None else str(actual.previous)
        return f"실제 {a}{unit} · 예측 {f}{unit} · 이전 {p}{unit}"
    if event.category == "disclosure":
        return event.company_name
    return None


def _event_title(event: MarketEventResponse) -> str:
    if event.title:
        return event.title
    if event.symbol:
        return f"{event.symbol} 이벤트"
    return "이벤트"


def _korean_weekday(d: date) -> str:
    return KO_WEEKDAYS[d.weekday()]


def _week_label(d: date) -> str:
    monday = d - timedelta(days=d.weekday())
    week_index = (monday.day - 1) // 7 + 1
    if monday.month != d.month:
        week_index = 1
    return f"{d.month}월 {week_index}주차"


def _filter_by_tab(
    events: list[MarketEventResponse], tab: Tab
) -> list[MarketEventResponse]:
    if tab == "all":
        return events
    if tab == "economic":
        return [e for e in events if e.category == "economic"]
    if tab == "earnings":
        return [e for e in events if e.category == "earnings"]
    return events


@dataclass
class _Scored:
    priority: Priority
    event: MarketEventResponse


class DiscoverCalendarService:
    def __init__(self, query_service: MarketEventsQueryService) -> None:
        self.query_service = query_service

    async def build(
        self,
        *,
        from_date: date,
        to_date: date,
        today: date,
        ctx: UserEventContext,
        tab: Tab = "all",
    ) -> DiscoverCalendarResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        range_resp = await self.query_service.list_for_range(from_date, to_date)
        filtered = _filter_by_tab(range_resp.events, tab)

        scored: list[_Scored] = [
            _Scored(priority=compute_priority(e, ctx), event=e) for e in filtered
        ]

        by_date: dict[date, list[_Scored]] = {}
        for s in scored:
            by_date.setdefault(s.event.event_date, []).append(s)

        days: list[DiscoverCalendarDay] = []
        cursor = from_date
        high_importance_count = 0
        while cursor <= to_date:
            bucket = by_date.get(cursor, [])
            bucket.sort(
                key=lambda s: (
                    s.priority.value,
                    _event_sort_time(s.event),
                    s.event.symbol or "",
                )
            )
            high_importance_count += sum(
                1
                for s in bucket
                if s.priority
                in (
                    Priority.HELD,
                    Priority.WATCHED,
                    Priority.MAJOR,
                    Priority.HIGH_IMPORTANCE,
                )
            )
            visible = bucket[:PER_DAY_VISIBLE_LIMIT]
            hidden = max(0, len(bucket) - PER_DAY_VISIBLE_LIMIT)
            days.append(
                DiscoverCalendarDay(
                    date=cursor,
                    weekday=_korean_weekday(cursor),
                    is_today=(cursor == today),
                    events=[
                        DiscoverCalendarEvent(
                            title=_event_title(s.event),
                            badge=PRIORITY_BADGE[s.priority],
                            category=s.event.category,
                            market=s.event.market,
                            symbol=s.event.symbol,
                            subtitle=_format_subtitle(s.event),
                            time_label=_format_time_label(s.event),
                            priority=PRIORITY_LABEL[s.priority],
                            source_event_id=s.event.source_event_id,
                        )
                        for s in visible
                    ],
                    hidden_count=hidden,
                )
            )
            cursor += timedelta(days=1)

        headline: str | None = None
        if high_importance_count > 0:
            headline = (
                f"이번 주 주요 이벤트 {high_importance_count}건이 예정되어 있어요"
            )

        return DiscoverCalendarResponse(
            headline=headline,
            week_label=_week_label(today),
            from_date=from_date,
            to_date=to_date,
            today=today,
            tab=tab,
            days=days,
        )
