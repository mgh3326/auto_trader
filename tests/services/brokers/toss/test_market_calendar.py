from __future__ import annotations

import datetime as dt

import pytest

from app.services.brokers.toss.market_calendar import (
    clear_toss_market_calendar_cache,
    get_toss_market_day,
    kr_nxt_session_for,
    kr_toss_session_for,
    parse_kr_market_calendar,
    parse_us_market_calendar,
    us_toss_session_for,
)

KST = dt.timezone(dt.timedelta(hours=9))


def _session(start: str, end: str, **extra: str | None) -> dict[str, str | None]:
    return {"startTime": start, "endTime": end, **extra}


def test_parse_kr_calendar_reads_nxt_after_1530() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": _session(
                    "2026-03-25T08:00:00+09:00",
                    "2026-03-25T09:00:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T08:50:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T15:20:00+09:00",
                ),
                "afterMarket": _session(
                    "2026-03-25T15:30:00+09:00",
                    "2026-03-25T20:00:00+09:00",
                    singlePriceAuctionEndTime="2026-03-25T15:40:00+09:00",
                ),
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }

    calendar = parse_kr_market_calendar(raw)
    today = calendar.day_for(dt.date(2026, 3, 25))

    assert today is not None
    assert today.after_market is not None
    assert today.after_market.start == dt.datetime(2026, 3, 25, 15, 30, tzinfo=KST)
    assert today.after_market.end == dt.datetime(2026, 3, 25, 20, 0, tzinfo=KST)


def test_kr_nxt_session_for_partial_nxt_holiday_returns_none() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": None,
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime=None,
                ),
                "afterMarket": None,
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }
    calendar = parse_kr_market_calendar(raw)

    assert (
        kr_nxt_session_for(
            dt.datetime(2026, 3, 25, 15, 45, tzinfo=KST), calendar=calendar
        )
        is None
    )


def test_kr_toss_session_for_regular_market() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": _session(
                    "2026-03-25T08:00:00+09:00",
                    "2026-03-25T09:00:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T08:50:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T15:20:00+09:00",
                ),
                "afterMarket": _session(
                    "2026-03-25T15:30:00+09:00",
                    "2026-03-25T20:00:00+09:00",
                    singlePriceAuctionEndTime="2026-03-25T15:40:00+09:00",
                ),
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }
    calendar = parse_kr_market_calendar(raw)

    assert (
        kr_toss_session_for(
            dt.datetime(2026, 3, 25, 9, 3, tzinfo=KST), calendar=calendar
        )
        == "regular"
    )
    assert (
        kr_nxt_session_for(
            dt.datetime(2026, 3, 25, 9, 3, tzinfo=KST), calendar=calendar
        )
        is None
    )


def test_parse_us_calendar_reads_day_market_without_persisted_session_literal() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "dayMarket": _session(
                "2026-03-25T09:00:00+09:00",
                "2026-03-25T16:50:00+09:00",
            ),
            "preMarket": _session(
                "2026-03-25T17:00:00+09:00",
                "2026-03-25T22:30:00+09:00",
            ),
            "regularMarket": _session(
                "2026-03-25T22:30:00+09:00",
                "2026-03-26T05:00:00+09:00",
            ),
            "afterMarket": _session(
                "2026-03-26T05:00:00+09:00",
                "2026-03-26T07:00:00+09:00",
            ),
        },
        "previousBusinessDay": {"date": "2026-03-24"},
        "nextBusinessDay": {"date": "2026-03-26"},
    }

    calendar = parse_us_market_calendar(raw)

    assert (
        us_toss_session_for(
            dt.datetime(2026, 3, 25, 10, 0, tzinfo=KST), calendar=calendar
        )
        == "day"
    )
    assert (
        us_toss_session_for(
            dt.datetime(2026, 3, 25, 18, 0, tzinfo=KST), calendar=calendar
        )
        == "pre"
    )


@pytest.mark.asyncio
async def test_get_toss_market_day_uses_one_kst_day_cache(monkeypatch) -> None:
    clear_toss_market_calendar_cache()
    calls: list[str | None] = []

    class Client:
        async def market_calendar_kr(self, *, date: str | None = None):
            calls.append(date)
            return {
                "today": {"date": "2026-03-25", "integrated": None},
                "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
                "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
            }

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        "app.services.brokers.toss.market_calendar.TossReadClient.from_settings",
        lambda: Client(),
    )

    first = await get_toss_market_day("kr", dt.date(2026, 3, 25))
    second = await get_toss_market_day("kr", dt.date(2026, 3, 25))

    assert first is second
    assert calls == ["2026-03-25"]
