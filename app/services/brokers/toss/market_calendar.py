from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.services.brokers.toss.client import TossReadClient

logger = logging.getLogger(__name__)

Market = Literal["kr", "us"]
KrNxtSession = Literal["nxt_premarket", "nxt_after", "closed"]
KrTossSession = Literal["nxt_premarket", "regular", "nxt_after", "closed"]
UsTossSession = Literal["day", "pre", "regular", "post"]

_KST = dt.timezone(dt.timedelta(hours=9))
_CACHE: dict[tuple[Market, dt.date], tuple[dt.date, TossMarketCalendar]] = {}


@dataclass(frozen=True)
class TossSessionWindow:
    start: dt.datetime
    end: dt.datetime
    single_price_auction_start: dt.datetime | None = None
    single_price_auction_end: dt.datetime | None = None

    def contains(self, moment: dt.datetime) -> bool:
        local = _to_kst(moment)
        return self.start <= local < self.end


@dataclass(frozen=True)
class TossKrMarketDay:
    date: dt.date
    pre_market: TossSessionWindow | None
    regular_market: TossSessionWindow | None
    after_market: TossSessionWindow | None


@dataclass(frozen=True)
class TossUsMarketDay:
    date: dt.date
    day_market: TossSessionWindow | None
    pre_market: TossSessionWindow | None
    regular_market: TossSessionWindow | None
    after_market: TossSessionWindow | None


@dataclass(frozen=True)
class TossMarketCalendar:
    market: Market
    days: tuple[TossKrMarketDay | TossUsMarketDay, ...]

    def day_for(self, day: dt.date) -> TossKrMarketDay | TossUsMarketDay | None:
        for item in self.days:
            if item.date == day:
                return item
        return None


def clear_toss_market_calendar_cache() -> None:
    _CACHE.clear()


def _to_kst(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_KST)
    return moment.astimezone(_KST)


def _parse_datetime(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Toss calendar datetime must be a string or null")
    return dt.datetime.fromisoformat(value).astimezone(_KST)


def _parse_window(raw: dict[str, Any] | None) -> TossSessionWindow | None:
    if raw is None:
        return None
    return TossSessionWindow(
        start=_parse_datetime(raw["startTime"]) or _missing_datetime("startTime"),
        end=_parse_datetime(raw["endTime"]) or _missing_datetime("endTime"),
        single_price_auction_start=_parse_datetime(
            raw.get("singlePriceAuctionStartTime")
        ),
        single_price_auction_end=_parse_datetime(raw.get("singlePriceAuctionEndTime")),
    )


def _missing_datetime(field_name: str) -> dt.datetime:
    raise ValueError(f"Toss calendar missing required {field_name}")


def _calendar_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    if "today" in raw and raw["today"] is not None:
        items.append(dict(raw["today"]))
    if "previousBusinessDay" in raw and raw["previousBusinessDay"] is not None:
        items.append(dict(raw["previousBusinessDay"]))
    if "nextBusinessDay" in raw and raw["nextBusinessDay"] is not None:
        items.append(dict(raw["nextBusinessDay"]))
    return items


def parse_kr_market_calendar(raw: dict[str, Any]) -> TossMarketCalendar:
    days: list[TossKrMarketDay] = []
    for item in _calendar_items(raw):
        integrated = item.get("integrated") or {}
        days.append(
            TossKrMarketDay(
                date=dt.date.fromisoformat(str(item["date"])),
                pre_market=_parse_window(integrated.get("preMarket")),
                regular_market=_parse_window(integrated.get("regularMarket")),
                after_market=_parse_window(integrated.get("afterMarket")),
            )
        )
    return TossMarketCalendar(market="kr", days=tuple(days))


def parse_us_market_calendar(raw: dict[str, Any]) -> TossMarketCalendar:
    days: list[TossUsMarketDay] = []
    for item in _calendar_items(raw):
        days.append(
            TossUsMarketDay(
                date=dt.date.fromisoformat(str(item["date"])),
                day_market=_parse_window(item.get("dayMarket")),
                pre_market=_parse_window(item.get("preMarket")),
                regular_market=_parse_window(item.get("regularMarket")),
                after_market=_parse_window(item.get("afterMarket")),
            )
        )
    return TossMarketCalendar(market="us", days=tuple(days))


def kr_nxt_session_for(
    moment: dt.datetime, *, calendar: TossMarketCalendar
) -> KrNxtSession | None:
    session = kr_toss_session_for(moment, calendar=calendar)
    if session in {"nxt_premarket", "nxt_after"}:
        return session
    return None


def kr_toss_session_for(
    moment: dt.datetime, *, calendar: TossMarketCalendar
) -> KrTossSession | None:
    local = _to_kst(moment)
    day = calendar.day_for(local.date())
    if not isinstance(day, TossKrMarketDay):
        return None
    if day.pre_market is not None and day.pre_market.contains(local):
        return "nxt_premarket"
    if day.regular_market is not None and day.regular_market.contains(local):
        return "regular"
    if day.after_market is not None and day.after_market.contains(local):
        return "nxt_after"
    return None


def us_toss_session_for(
    moment: dt.datetime, *, calendar: TossMarketCalendar
) -> UsTossSession | None:
    local = _to_kst(moment)
    for day in calendar.days:
        if not isinstance(day, TossUsMarketDay):
            continue
        if day.day_market is not None and day.day_market.contains(local):
            return "day"
        if day.pre_market is not None and day.pre_market.contains(local):
            return "pre"
        if day.regular_market is not None and day.regular_market.contains(local):
            return "regular"
        if day.after_market is not None and day.after_market.contains(local):
            return "post"
    return None


async def get_toss_market_calendar(
    market: Market, query_date: dt.date
) -> TossMarketCalendar | None:
    fetched_on = dt.datetime.now(_KST).date()
    key = (market, query_date)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == fetched_on:
        return cached[1]

    try:
        client = TossReadClient.from_settings()
        try:
            if market == "kr":
                raw = await client.market_calendar_kr(date=query_date.isoformat())
                parsed = parse_kr_market_calendar(raw)
            else:
                raw = await client.market_calendar_us(date=query_date.isoformat())
                parsed = parse_us_market_calendar(raw)
        finally:
            await client.aclose()
    except Exception:
        logger.info("Toss market calendar unavailable for %s %s", market, query_date)
        return None

    _CACHE[key] = (fetched_on, parsed)
    return parsed


async def get_toss_market_day(
    market: Market, day: dt.date
) -> TossKrMarketDay | TossUsMarketDay | None:
    calendar = await get_toss_market_calendar(market, day)
    if calendar is None:
        return None
    return calendar.day_for(day)


async def get_kr_nxt_session_from_toss(moment: dt.datetime) -> KrNxtSession | None:
    local = _to_kst(moment)
    calendar = await get_toss_market_calendar("kr", local.date())
    if calendar is None:
        return None
    return kr_nxt_session_for(local, calendar=calendar) or "closed"


async def get_kr_toss_session_from_toss(moment: dt.datetime) -> KrTossSession | None:
    local = _to_kst(moment)
    calendar = await get_toss_market_calendar("kr", local.date())
    if calendar is None:
        return None
    return kr_toss_session_for(local, calendar=calendar) or "closed"


async def get_us_toss_session_from_toss(moment: dt.datetime) -> UsTossSession | None:
    local = _to_kst(moment)
    calendar = await get_toss_market_calendar("us", local.date())
    if calendar is None:
        return None
    return us_toss_session_for(local, calendar=calendar)
