"""TradingView fetch helper tests (ROB-210).

All tests use fixture payloads — no live network calls in CI.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

SAMPLE_TV_PAYLOAD = {
    "result": [
        {
            "id": "4b8e4f00-0e49-4a4a-b6e2-111111111111",
            "title": "Core CPI m/m",
            "country": "US",
            "date": "2026-05-13T12:30:00Z",
            "period": "Apr",
            "actual": "0.3",
            "forecast": "0.3",
            "previous": "0.4",
            "unit": "%",
            "source": "Bureau of Labor Statistics",
            "source_url": "https://www.bls.gov/cpi/",
            "ticker": None,
            "importance": 3,
        },
        {
            "id": "4b8e4f00-0e49-4a4a-b6e2-222222222222",
            "title": "Industrial Production m/m",
            "country": "DE",
            "date": "2026-05-13T06:00:00Z",
            "period": "Mar",
            "actual": None,
            "forecast": "-0.5",
            "previous": "1.2",
            "unit": "%",
            "source": "Destatis",
            "source_url": "https://www.destatis.de/",
            "ticker": None,
            "importance": 2,
        },
        {
            "id": "4b8e4f00-0e49-4a4a-b6e2-333333333333",
            "title": "Bank Holiday",
            "country": "JP",
            "date": "2026-05-14T00:00:00Z",
            "period": None,
            "actual": None,
            "forecast": None,
            "previous": None,
            "unit": None,
            "source": None,
            "source_url": None,
            "ticker": None,
            "importance": 0,
        },
    ],
    "status": "ok",
}

SAMPLE_TV_PAYLOAD_LIST = [
    {
        "id": "abc-123",
        "title": "GDP Growth Rate",
        "country": "US",
        "date": "2026-05-13T12:30:00Z",
        "period": "Q1",
        "actual": "2.5",
        "forecast": "2.4",
        "previous": "2.3",
        "unit": "%",
        "source": "BEA",
        "source_url": None,
        "ticker": None,
        "importance": 3,
    }
]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_tradingview_filters_to_target_day():
    from app.services.market_events import tradingview_helpers as tv

    with patch.object(
        tv, "_fetch_tradingview_raw", AsyncMock(return_value=SAMPLE_TV_PAYLOAD)
    ):
        rows = await tv.fetch_tradingview_events_for_date(date(2026, 5, 13))

    assert len(rows) == 2
    titles = {r["title"] for r in rows}
    assert titles == {"Core CPI m/m", "Industrial Production m/m"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_tradingview_excludes_other_days():
    from app.services.market_events import tradingview_helpers as tv

    with patch.object(
        tv, "_fetch_tradingview_raw", AsyncMock(return_value=SAMPLE_TV_PAYLOAD)
    ):
        rows = await tv.fetch_tradingview_events_for_date(date(2026, 5, 14))

    assert len(rows) == 1
    assert rows[0]["title"] == "Bank Holiday"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_tradingview_parses_utc_datetime():
    from app.services.market_events import tradingview_helpers as tv

    with patch.object(
        tv, "_fetch_tradingview_raw", AsyncMock(return_value=SAMPLE_TV_PAYLOAD)
    ):
        rows = await tv.fetch_tradingview_events_for_date(date(2026, 5, 13))

    cpi = next(r for r in rows if r["title"] == "Core CPI m/m")
    assert cpi["date_utc"] == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    assert cpi["country"] == "US"
    assert cpi["importance"] == 3
    assert cpi["unit"] == "%"
    assert cpi["id"] == "4b8e4f00-0e49-4a4a-b6e2-111111111111"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_tradingview_accepts_list_payload():
    from app.services.market_events import tradingview_helpers as tv

    with patch.object(
        tv, "_fetch_tradingview_raw", AsyncMock(return_value=SAMPLE_TV_PAYLOAD_LIST)
    ):
        rows = await tv.fetch_tradingview_events_for_date(date(2026, 5, 13))

    assert len(rows) == 1
    assert rows[0]["title"] == "GDP Growth Rate"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_tradingview_raises_on_network_error():
    from app.services.market_events import tradingview_helpers as tv

    with patch.object(
        tv, "_fetch_tradingview_raw", AsyncMock(side_effect=TimeoutError("timeout"))
    ):
        with pytest.raises(TimeoutError):
            await tv.fetch_tradingview_events_for_date(date(2026, 5, 13))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_tradingview_raw_sends_browser_origin_headers():
    from app.services.market_events import tradingview_helpers as tv

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok", "result": []}

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, params=None, headers=None):
            self.calls.append({"url": url, "params": params, "headers": headers})
            return FakeResponse()

    fake_client = FakeClient()
    with patch.object(tv.httpx, "AsyncClient", return_value=fake_client):
        payload = await tv._fetch_tradingview_raw(date(2026, 5, 13), date(2026, 5, 13))

    assert payload == {"status": "ok", "result": []}
    assert fake_client.calls[0]["url"] == tv.TRADINGVIEW_CALENDAR_URL
    assert fake_client.calls[0]["headers"]["Origin"] == "https://www.tradingview.com"
    assert (
        fake_client.calls[0]["headers"]["Referer"]
        == "https://www.tradingview.com/economic-calendar/"
    )
    assert "Mozilla/5.0" in fake_client.calls[0]["headers"]["User-Agent"]


@pytest.mark.unit
def test_parse_tv_rows_skips_items_without_parseable_date():
    from app.services.market_events.tradingview_helpers import _parse_tv_rows

    payload = {
        "result": [
            {
                "id": "ok",
                "title": "Good Event",
                "date": "2026-05-13T12:30:00Z",
                "country": "US",
            },
            {"id": "bad", "title": "No Date"},
            {"id": "bad2", "title": "Garbage Date", "date": "not-a-date"},
        ]
    }
    rows = _parse_tv_rows(payload)
    assert len(rows) == 1
    assert rows[0]["title"] == "Good Event"


@pytest.mark.unit
def test_parse_tv_date_handles_unix_timestamp():
    from app.services.market_events.tradingview_helpers import _parse_tv_date

    # 2026-05-13T12:30:00Z as Unix timestamp
    ts = datetime(2026, 5, 13, 12, 30, tzinfo=UTC).timestamp()
    result = _parse_tv_date(int(ts))
    assert result is not None
    assert result.year == 2026
    assert result.month == 5
    assert result.day == 13
    assert result.tzinfo == UTC


@pytest.mark.unit
def test_parse_tv_date_handles_iso_string():
    from app.services.market_events.tradingview_helpers import _parse_tv_date

    result = _parse_tv_date("2026-05-13T12:30:00Z")
    assert result == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)


@pytest.mark.unit
def test_parse_tv_date_returns_none_for_none():
    from app.services.market_events.tradingview_helpers import _parse_tv_date

    assert _parse_tv_date(None) is None
    assert _parse_tv_date("") is None
