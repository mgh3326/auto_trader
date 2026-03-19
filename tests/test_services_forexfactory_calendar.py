import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.external.forexfactory_calendar import (
    FOREXFACTORY_NEXTWEEK_URL,
    FOREXFACTORY_THISWEEK_URL,
    fetch_forexfactory_events_today,
)

# Mock KST timezone as in app.core.timezone
KST = timezone(timedelta(hours=9))

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_filters_today_and_normalizes_fields() -> None:
    # Set "today" to March 19, 2026 (Thursday)
    today = datetime(2026, 3, 19, 12, 0, tzinfo=KST)

    xml_content = """
<weeklyevents>
  <event>
    <title>Core CPI m/m</title>
    <country>USD</country>
    <date>03-19-2026</date>
    <time>8:30am</time>
    <impact>High</impact>
    <forecast>0.3%</forecast>
    <previous>0.4%</previous>
    <actual></actual>
  </event>
  <event>
    <title>Yesterday Event</title>
    <country>USD</country>
    <date>03-18-2026</date>
    <time>9:00am</time>
    <impact>Low</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
</weeklyevents>
"""
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text=xml_content)

            events = await fetch_forexfactory_events_today()

            assert len(events) == 1
            event = events[0]
            assert event["event"] == "Core CPI m/m"
            assert event["country"] == "USD"
            assert event["time"] == "22:30 KST"  # 8:30am + 14h
            assert event["impact"] == "high"
            assert event["forecast"] == "0.3%"
            assert event["previous"] == "0.4%"
            assert event["actual"] is None

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_converts_blank_values_to_none() -> None:
    today = datetime(2026, 3, 19, 12, 0, tzinfo=KST)
    xml_content = """
<weeklyevents>
  <event>
    <title>Empty Values Event</title>
    <country>USD</country>
    <date>03-19-2026</date>
    <time>8:00am</time>
    <impact>Medium</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
</weeklyevents>
"""
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text=xml_content)

            events = await fetch_forexfactory_events_today()

            assert len(events) == 1
            assert events[0]["forecast"] is None
            assert events[0]["previous"] is None
            assert events[0]["actual"] is None

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_excludes_events_that_roll_over_to_next_kst_day() -> None:
    today = datetime(2026, 3, 19, 12, 0, tzinfo=KST)
    xml_content = """
<weeklyevents>
  <event>
    <title>Noon Event</title>
    <country>USD</country>
    <date>03-19-2026</date>
    <time>12:00pm</time>
    <impact>Low</impact>
  </event>
</weeklyevents>
"""
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            response = MagicMock()
            response.text = xml_content
            response.raise_for_status.return_value = None
            mock_get.return_value = response

            events = await fetch_forexfactory_events_today()

            assert events == []

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_includes_previous_et_day_events_that_roll_into_today_kst() -> None:
    today = datetime(2026, 3, 19, 7, 0, tzinfo=KST)
    xml_content = """
<weeklyevents>
  <event>
    <title>Late ET Event</title>
    <country>USD</country>
    <date>03-18-2026</date>
    <time>10:30pm</time>
    <impact>High</impact>
  </event>
</weeklyevents>
"""
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            response = MagicMock()
            response.text = xml_content
            response.raise_for_status.return_value = None
            mock_get.return_value = response

            events = await fetch_forexfactory_events_today()

            assert len(events) == 1
            assert events[0]["event"] == "Late ET Event"
            assert events[0]["time"] == "12:30 KST"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_raises_on_http_failure() -> None:
    today = datetime(2026, 3, 19, 12, 0, tzinfo=KST)
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.HTTPError("boom")):
            with pytest.raises(httpx.HTTPError):
                await fetch_forexfactory_events_today()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_raises_on_malformed_xml() -> None:
    today = datetime(2026, 3, 19, 12, 0, tzinfo=KST)
    bad_xml = "<weeklyevents><event><title>broken"
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            response = MagicMock()
            response.text = bad_xml
            response.raise_for_status.return_value = None
            mock_get.return_value = response

            with pytest.raises(ET.ParseError):
                await fetch_forexfactory_events_today()

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_handles_tentative_all_day_as_midnight() -> None:
    today = datetime(2026, 3, 19, 12, 0, tzinfo=KST)
    xml_content = """
<weeklyevents>
  <event>
    <title>Tentative Event</title>
    <country>USD</country>
    <date>03-19-2026</date>
    <time>Tentative</time>
    <impact>Low</impact>
  </event>
  <event>
    <title>All Day Event</title>
    <country>USD</country>
    <date>03-19-2026</date>
    <time>All Day</time>
    <impact>Low</impact>
  </event>
</weeklyevents>
"""
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text=xml_content)

            events = await fetch_forexfactory_events_today()

            for event in events:
                assert event["time"] == "00:00 KST"

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_forexfactory_events_today_fetches_nextweek_on_friday() -> None:
    # Friday, March 20, 2026
    today = datetime(2026, 3, 20, 12, 0, tzinfo=KST)

    this_week_xml = """
<weeklyevents>
  <event>
    <title>Friday Event</title>
    <country>USD</country>
    <date>03-20-2026</date>
    <time>9:00am</time>
    <impact>High</impact>
  </event>
</weeklyevents>
"""
    next_week_xml = """
<weeklyevents>
  <event>
    <title>Future Event</title>
    <country>USD</country>
    <date>03-21-2026</date>
    <time>9:00am</time>
    <impact>High</impact>
  </event>
</weeklyevents>
"""
    with patch("app.services.external.forexfactory_calendar.now_kst", return_value=today):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            def side_effect(url, **kwargs):
                if url == FOREXFACTORY_THISWEEK_URL:
                    return MagicMock(status_code=200, text=this_week_xml)
                elif url == FOREXFACTORY_NEXTWEEK_URL:
                    return MagicMock(status_code=200, text=next_week_xml)
                return MagicMock(status_code=404)

            mock_get.side_effect = side_effect

            events = await fetch_forexfactory_events_today()

            assert len(events) == 1
            assert events[0]["event"] == "Friday Event"
            assert mock_get.call_count == 2
