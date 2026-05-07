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
