"""ForexFactory per-day fetch helper tests (ROB-132)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<weeklyevents>
  <event>
    <title>Core CPI m/m</title>
    <country>USD</country>
    <date>05-13-2026</date>
    <time>8:30am</time>
    <impact>High</impact>
    <forecast>0.3%</forecast>
    <previous>0.4%</previous>
    <actual>0.3%</actual>
  </event>
  <event>
    <title>Trade Balance</title>
    <country>EUR</country>
    <date>05-13-2026</date>
    <time>5:00am</time>
    <impact>Low</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
  <event>
    <title>Bank Holiday</title>
    <country>JPY</country>
    <date>05-14-2026</date>
    <time>All Day</time>
    <impact>Holiday</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
</weeklyevents>
"""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_for_date_filters_to_target_day():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=[SAMPLE_XML])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 13))

    assert len(rows) == 2
    assert {r["title"] for r in rows} == {"Core CPI m/m", "Trade Balance"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_converts_et_to_utc_and_emits_stable_id():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=[SAMPLE_XML])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 13))

    cpi = next(r for r in rows if r["title"] == "Core CPI m/m")
    # 8:30am ET in May (EDT, UTC-4) -> 12:30 UTC.
    assert cpi["release_time_utc"] == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    assert cpi["currency"] == "USD"
    assert cpi["impact"] == "high"
    assert cpi["source_event_id"].startswith("ff::USD::Core CPI m/m::")
    assert "2026-05-13T12:30" in cpi["source_event_id"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_handles_all_day_no_time():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=[SAMPLE_XML])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 14))

    assert len(rows) == 1
    holiday = rows[0]
    assert holiday["release_time_utc"] is None
    assert holiday["release_time_local"] is None
    assert holiday["time_hint_raw"].lower() in ("all day", "all_day", "tentative", "")
    assert holiday["source_event_id"].startswith("ff::JPY::Bank Holiday::2026-05-14")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_returns_empty_on_xml_error():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=["not-xml"])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 13))

    assert rows == []


# ---------------------------------------------------------------------------
# ROB-184: rolling_window_for_today tests
# ---------------------------------------------------------------------------

from zoneinfo import ZoneInfo  # noqa: E402

ET = ZoneInfo("America/New_York")


@pytest.mark.unit
def test_rolling_window_for_today_is_two_iso_weeks_in_et():
    from app.services.market_events.forexfactory_helpers import (
        rolling_window_for_today,
    )

    # Tue 2026-05-12 06:00 UTC == 02:00 ET (still Monday in ET? no — Tue)
    now_utc = datetime(2026, 5, 12, 6, 0, tzinfo=UTC)
    start, end = rolling_window_for_today(now_utc)
    # ISO-week containing 2026-05-12 ET starts Mon 2026-05-11; next week
    # ends Sun 2026-05-24. We anchor on Mon..Sun(+7) to match upstream feed.
    assert start == date(2026, 5, 11)
    assert end == date(2026, 5, 24)


@pytest.mark.unit
def test_rolling_window_for_today_handles_sunday_et_boundary():
    from app.services.market_events.forexfactory_helpers import (
        rolling_window_for_today,
    )

    # 2026-05-11 03:30 UTC == 2026-05-10 23:30 ET (Sunday)
    now_utc = datetime(2026, 5, 11, 3, 30, tzinfo=UTC)
    start, end = rolling_window_for_today(now_utc)
    # Sunday still belongs to "this week" feed (Mon 2026-05-04 .. Sun 2026-05-10)
    assert start == date(2026, 5, 4)
    assert end == date(2026, 5, 17)


# ---------------------------------------------------------------------------
# ROB-184: typed fetch error + retry wrapper tests
# ---------------------------------------------------------------------------

import httpx  # noqa: E402


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_xml_retries_429_with_retry_after_header(monkeypatch):
    from app.services.market_events import forexfactory_helpers as ff

    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, text="<weeklyevents/>", retry_after=None):
            self.status_code = status
            self.text = text
            self.headers = {"Retry-After": retry_after} if retry_after else {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "boom", request=httpx.Request("GET", "x"), response=self
                )

    async def fake_get(self, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, retry_after="0")
        return _Resp(200, text="<weeklyevents/>")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    # _fetch_one_xml is the new low-level helper exposed for the cache.
    text = await ff._fetch_one_xml(ff.THISWEEK_URL, max_attempts=3, base_delay=0)
    assert calls["n"] == 2
    assert text.startswith("<weeklyevents")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_xml_raises_forexfactory_fetch_error_on_429_exhaustion(
    monkeypatch,
):
    from app.services.market_events import forexfactory_helpers as ff

    async def always_429(self, url, **kw):
        class _R:
            status_code = 429
            headers = {"Retry-After": "0"}
            text = ""

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "x", request=httpx.Request("GET", url), response=self
                )

        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "get", always_429)
    with pytest.raises(ff.ForexFactoryFetchError) as exc_info:
        await ff._fetch_one_xml(ff.THISWEEK_URL, max_attempts=3, base_delay=0)
    assert exc_info.value.reason == "rate_limited"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_xml_does_not_retry_on_403(monkeypatch):
    from app.services.market_events import forexfactory_helpers as ff

    async def fake_get(self, url, **kw):
        class _R:
            status_code = 403
            headers = {}
            text = ""

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "x", request=httpx.Request("GET", url), response=self
                )

        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ff.ForexFactoryFetchError) as exc:
        await ff._fetch_one_xml(ff.THISWEEK_URL, max_attempts=3, base_delay=0)
    assert exc.value.reason == "upstream_4xx"
