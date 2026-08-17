"""Tests for the read-only Toss-backed market-calendar router (ROB-1280)."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import market_calendar
from app.services.brokers.toss.market_calendar import (
    TossKrMarketDay,
    TossMarketCalendar,
    TossSessionWindow,
    TossUsMarketDay,
)

_KST = dt.timezone(dt.timedelta(hours=9))


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(market_calendar.router)
    return app


def _kr_window(hour_start: int, hour_end: int, day: dt.date) -> TossSessionWindow:
    return TossSessionWindow(
        start=dt.datetime(day.year, day.month, day.day, hour_start, tzinfo=_KST),
        end=dt.datetime(day.year, day.month, day.day, hour_end, tzinfo=_KST),
    )


@pytest.mark.unit
def test_toss_disabled_returns_unavailable(monkeypatch):
    monkeypatch.setattr(market_calendar.settings, "toss_api_enabled", False)
    client = TestClient(_app())

    resp = client.get("/trading/api/market-calendar/kr/today")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_open"] is None
    assert data["unavailable_reason"] == "toss_api_disabled"
    assert data["source"] == "toss_calendar"


@pytest.mark.unit
def test_calendar_fetch_failure_returns_unavailable(monkeypatch):
    monkeypatch.setattr(market_calendar.settings, "toss_api_enabled", True)

    async def _fake_get_calendar(market, query_date):
        return None

    monkeypatch.setattr(market_calendar, "get_toss_market_calendar", _fake_get_calendar)
    client = TestClient(_app())

    resp = client.get("/trading/api/market-calendar/kr/today")

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_open"] is None
    assert data["unavailable_reason"] == "toss_calendar_unavailable"


@pytest.mark.unit
def test_kr_holiday_reports_closed(monkeypatch):
    monkeypatch.setattr(market_calendar.settings, "toss_api_enabled", True)
    query_date = dt.date(2026, 8, 17)
    day = TossKrMarketDay(
        date=query_date,
        pre_market=None,
        regular_market=None,
        after_market=None,
    )
    calendar = TossMarketCalendar(market="kr", days=(day,))

    async def _fake_get_calendar(market, qd):
        assert market == "kr"
        assert qd == query_date
        return calendar

    monkeypatch.setattr(market_calendar, "get_toss_market_calendar", _fake_get_calendar)
    client = TestClient(_app())

    resp = client.get(
        "/trading/api/market-calendar/kr/today", params={"date": "2026-08-17"}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == "2026-08-17"
    assert data["is_open"] is False
    assert data["unavailable_reason"] is None


@pytest.mark.unit
def test_kr_trading_day_reports_open_with_windows(monkeypatch):
    monkeypatch.setattr(market_calendar.settings, "toss_api_enabled", True)
    query_date = dt.date(2026, 8, 18)
    day = TossKrMarketDay(
        date=query_date,
        pre_market=_kr_window(8, 9, query_date),
        regular_market=_kr_window(9, 15, query_date),
        after_market=_kr_window(16, 20, query_date),
    )
    calendar = TossMarketCalendar(market="kr", days=(day,))

    async def _fake_get_calendar(market, qd):
        return calendar

    monkeypatch.setattr(market_calendar, "get_toss_market_calendar", _fake_get_calendar)
    client = TestClient(_app())

    resp = client.get(
        "/trading/api/market-calendar/kr/today", params={"date": "2026-08-18"}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_open"] is True
    assert data["session_windows"]["regular_market"] is not None


@pytest.mark.unit
def test_us_labor_day_reports_closed(monkeypatch):
    monkeypatch.setattr(market_calendar.settings, "toss_api_enabled", True)
    query_date = dt.date(2026, 9, 7)
    day = TossUsMarketDay(
        date=query_date,
        day_market=None,
        pre_market=None,
        regular_market=None,
        after_market=None,
    )
    calendar = TossMarketCalendar(market="us", days=(day,))

    async def _fake_get_calendar(market, qd):
        assert market == "us"
        return calendar

    monkeypatch.setattr(market_calendar, "get_toss_market_calendar", _fake_get_calendar)
    client = TestClient(_app())

    resp = client.get(
        "/trading/api/market-calendar/us/today", params={"date": "2026-09-07"}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_open"] is False


@pytest.mark.unit
def test_date_outside_calendar_window_returns_unavailable(monkeypatch):
    monkeypatch.setattr(market_calendar.settings, "toss_api_enabled", True)
    other_day = TossKrMarketDay(
        date=dt.date(2026, 8, 18),
        pre_market=None,
        regular_market=_kr_window(9, 15, dt.date(2026, 8, 18)),
        after_market=None,
    )
    calendar = TossMarketCalendar(market="kr", days=(other_day,))

    async def _fake_get_calendar(market, qd):
        return calendar

    monkeypatch.setattr(market_calendar, "get_toss_market_calendar", _fake_get_calendar)
    client = TestClient(_app())

    resp = client.get(
        "/trading/api/market-calendar/kr/today", params={"date": "2026-08-01"}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_open"] is None
    assert data["unavailable_reason"] == "date_out_of_calendar_window"
