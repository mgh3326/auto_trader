"""TradingView normalizer unit tests (ROB-210)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

TV_ROW_HIGH_IMPORTANCE = {
    "id": "4b8e4f00-0e49-4a4a-b6e2-111111111111",
    "title": "Core CPI m/m",
    "country": "US",
    "date_utc": datetime(2026, 5, 13, 12, 30, tzinfo=UTC),
    "period": "Apr",
    "actual": "0.3",
    "forecast": "0.3",
    "previous": "0.4",
    "unit": "%",
    "source": "Bureau of Labor Statistics",
    "source_url": "https://www.bls.gov/cpi/",
    "ticker": None,
    "importance": 3,
    "_raw": {
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
}

TV_ROW_SCHEDULED = {
    "id": "4b8e4f00-0e49-4a4a-b6e2-222222222222",
    "title": "Industrial Production m/m",
    "country": "DE",
    "date_utc": datetime(2026, 5, 13, 6, 0, tzinfo=UTC),
    "period": "Mar",
    "actual": None,
    "forecast": "-0.5",
    "previous": "1.2",
    "unit": "%",
    "source": "Destatis",
    "source_url": None,
    "ticker": None,
    "importance": 2,
    "_raw": {
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
        "source_url": None,
        "ticker": None,
        "importance": 2,
    },
}


@pytest.mark.unit
def test_normalize_tradingview_released_event():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    event, values = normalize_tradingview_event_row(TV_ROW_HIGH_IMPORTANCE)

    assert event["category"] == "economic"
    assert event["market"] == "global"
    assert event["country"] == "US"
    assert event["title"] == "Core CPI m/m"
    assert event["event_date"] == date(2026, 5, 13)
    assert event["release_time_utc"] == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    assert event["source_timezone"] == "UTC"
    assert event["time_hint"] == "unknown"
    assert event["importance"] == 3
    assert event["status"] == "released"
    assert event["source"] == "tradingview"
    assert event["source_event_id"] == "4b8e4f00-0e49-4a4a-b6e2-111111111111"
    assert event["source_url"] == "https://www.bls.gov/cpi/"
    assert event["fiscal_year"] is None
    assert event["fiscal_quarter"] is None
    assert event["symbol"] is None
    assert event["currency"] is None


@pytest.mark.unit
def test_normalize_tradingview_released_event_produces_values():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    _, values = normalize_tradingview_event_row(TV_ROW_HIGH_IMPORTANCE)

    assert len(values) == 1
    v = values[0]
    assert v["metric_name"] == "actual"
    assert v["period"] == "Apr"
    assert v["actual"] == Decimal("0.3")
    assert v["forecast"] == Decimal("0.3")
    assert v["previous"] == Decimal("0.4")
    assert v["unit"] == "%"


@pytest.mark.unit
def test_normalize_tradingview_scheduled_when_no_actual():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    event, values = normalize_tradingview_event_row(TV_ROW_SCHEDULED)

    assert event["status"] == "scheduled"
    assert event["importance"] == 2
    assert len(values) == 1
    v = values[0]
    assert v["actual"] is None
    assert v["forecast"] == Decimal("-0.5")
    assert v["previous"] == Decimal("1.2")


@pytest.mark.unit
def test_normalize_tradingview_scheduled_when_actual_is_dash():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    row = {**TV_ROW_HIGH_IMPORTANCE, "actual": "-"}
    event, _ = normalize_tradingview_event_row(row)
    assert event["status"] == "scheduled"


@pytest.mark.unit
def test_normalize_tradingview_importance_mapping():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    for raw, expected in [(1, 1), (2, 2), (3, 3), (0, None), (-1, None), (None, None)]:
        row = {**TV_ROW_HIGH_IMPORTANCE, "importance": raw}
        event, _ = normalize_tradingview_event_row(row)
        assert event["importance"] == expected, f"importance={raw}"


@pytest.mark.unit
def test_normalize_tradingview_emits_no_values_when_all_blank():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    row = {**TV_ROW_HIGH_IMPORTANCE, "actual": None, "forecast": None, "previous": None}
    _, values = normalize_tradingview_event_row(row)
    assert values == []


@pytest.mark.unit
def test_normalize_tradingview_requires_title_and_date_utc():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    with pytest.raises(ValueError):
        normalize_tradingview_event_row({**TV_ROW_HIGH_IMPORTANCE, "title": ""})

    with pytest.raises(ValueError):
        normalize_tradingview_event_row({**TV_ROW_HIGH_IMPORTANCE, "date_utc": None})


@pytest.mark.unit
def test_normalize_tradingview_fallback_source_event_id_when_no_id():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    row = {**TV_ROW_HIGH_IMPORTANCE, "id": None}
    event, _ = normalize_tradingview_event_row(row)
    assert event["source_event_id"].startswith("tv::US::Core CPI m/m::")
    assert "2026-05-13T12:30:00Z" in event["source_event_id"]


@pytest.mark.unit
def test_normalize_tradingview_ticker_becomes_symbol():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    row = {**TV_ROW_HIGH_IMPORTANCE, "ticker": "AAPL"}
    event, _ = normalize_tradingview_event_row(row)
    assert event["symbol"] == "AAPL"


@pytest.mark.unit
def test_normalize_tradingview_raw_payload_excludes_internal_fields():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    event, _ = normalize_tradingview_event_row(TV_ROW_HIGH_IMPORTANCE)

    raw = event["raw_payload_json"]
    assert "_raw" not in raw
    assert "date_utc" not in raw
    assert raw["id"] == "4b8e4f00-0e49-4a4a-b6e2-111111111111"
    assert raw["title"] == "Core CPI m/m"


@pytest.mark.unit
def test_normalize_tradingview_period_none_when_missing():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    row = {**TV_ROW_HIGH_IMPORTANCE, "period": None}
    _, values = normalize_tradingview_event_row(row)
    assert values[0]["period"] is None


@pytest.mark.unit
def test_normalize_tradingview_unit_none_when_missing():
    from app.services.market_events.normalizers import normalize_tradingview_event_row

    row = {**TV_ROW_HIGH_IMPORTANCE, "unit": None}
    _, values = normalize_tradingview_event_row(row)
    assert values[0]["unit"] is None
