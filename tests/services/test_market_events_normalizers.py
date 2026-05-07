"""Pure-function normalizer tests (ROB-128)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

FINNHUB_ROW_AMC = {
    "symbol": "IONQ",
    "date": "2026-05-07",
    "hour": "amc",
    "eps_estimate": -0.3593,
    "eps_actual": -0.38,
    "revenue_estimate": 50729332,
    "revenue_actual": 64670000,
    "quarter": 1,
    "year": 2026,
}

FINNHUB_ROW_BMO = {
    "symbol": "NVDA",
    "date": "2026-05-08",
    "hour": "bmo",
    "eps_estimate": 0.5,
    "eps_actual": None,
    "revenue_estimate": 1_000_000_000,
    "revenue_actual": None,
    "quarter": 1,
    "year": 2026,
}


@pytest.mark.unit
def test_normalize_finnhub_earnings_amc_returns_after_close_hint():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    event_dict, value_dicts = normalize_finnhub_earnings_row(FINNHUB_ROW_AMC)

    assert event_dict["category"] == "earnings"
    assert event_dict["market"] == "us"
    assert event_dict["symbol"] == "IONQ"
    assert event_dict["event_date"] == date(2026, 5, 7)
    assert event_dict["time_hint"] == "after_close"
    assert event_dict["source"] == "finnhub"
    assert event_dict["fiscal_year"] == 2026
    assert event_dict["fiscal_quarter"] == 1
    assert event_dict["status"] == "released"
    assert event_dict["source_event_id"] is None  # Finnhub does not provide id

    metrics = {v["metric_name"]: v for v in value_dicts}
    assert "eps" in metrics
    assert metrics["eps"]["actual"] == Decimal("-0.38")
    assert metrics["eps"]["forecast"] == Decimal("-0.3593")
    assert metrics["eps"]["unit"] == "USD"
    assert metrics["revenue"]["actual"] == Decimal("64670000")
    assert metrics["revenue"]["forecast"] == Decimal("50729332")


@pytest.mark.unit
def test_normalize_finnhub_earnings_bmo_returns_before_open_hint():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    event_dict, _ = normalize_finnhub_earnings_row(FINNHUB_ROW_BMO)

    assert event_dict["time_hint"] == "before_open"
    assert event_dict["status"] == "scheduled"  # actual is None


@pytest.mark.unit
def test_normalize_finnhub_earnings_unknown_hour_falls_back_to_unknown():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    row = {**FINNHUB_ROW_AMC, "hour": ""}
    event_dict, _ = normalize_finnhub_earnings_row(row)
    assert event_dict["time_hint"] == "unknown"


@pytest.mark.unit
def test_normalize_finnhub_earnings_skips_value_when_both_actual_and_forecast_missing():
    from app.services.market_events.normalizers import normalize_finnhub_earnings_row

    row = {
        **FINNHUB_ROW_AMC,
        "revenue_estimate": None,
        "revenue_actual": None,
    }
    _, value_dicts = normalize_finnhub_earnings_row(row)
    metrics = {v["metric_name"] for v in value_dicts}
    assert "revenue" not in metrics
    assert "eps" in metrics


DART_ROW_QUARTERLY = {
    "rcept_no": "20260507000123",
    "rcept_dt": "20260507",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "stock_code": "005930",
    "report_nm": "분기보고서 (2026.03)",
}

DART_ROW_OTHER = {
    "rcept_no": "20260507000456",
    "rcept_dt": "20260507",
    "corp_name": "현대차",
    "corp_code": "00164742",
    "stock_code": "005380",
    "report_nm": "감사인지정",
}


@pytest.mark.unit
def test_normalize_dart_quarterly_classifies_as_earnings():
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    event, values = normalize_dart_disclosure_row(DART_ROW_QUARTERLY)
    assert event["category"] == "earnings"
    assert event["market"] == "kr"
    assert event["source"] == "dart"
    assert event["source_event_id"] == "20260507000123"
    assert event["symbol"] == "005930"
    assert event["company_name"] == "삼성전자"
    assert "rcpNo=20260507000123" in event["source_url"]
    assert event["event_date"] == date(2026, 5, 7)
    assert values == []


@pytest.mark.unit
def test_normalize_dart_unrelated_filing_classifies_as_disclosure():
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    event, _ = normalize_dart_disclosure_row(DART_ROW_OTHER)
    assert event["category"] == "disclosure"


@pytest.mark.unit
def test_normalize_dart_without_stock_code_keeps_symbol_empty_not_corp_code():
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    row = {**DART_ROW_QUARTERLY}
    row.pop("stock_code")
    event, _ = normalize_dart_disclosure_row(row)
    assert event["symbol"] is None
    assert event["raw_payload_json"]["corp_code"] == "00126380"


FF_ROW_HIGH_IMPACT = {
    "title": "Core CPI m/m",
    "currency": "USD",
    "country": "US",
    "event_date": date(2026, 5, 13),
    "release_time_utc": datetime(2026, 5, 13, 12, 30, tzinfo=UTC),
    "release_time_local": datetime(2026, 5, 13, 8, 30),
    "time_hint_raw": "8:30am",
    "impact": "high",
    "actual": "0.3%",
    "forecast": "0.3%",
    "previous": "0.4%",
    "source_event_id": "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z",
}


@pytest.mark.unit
def test_normalize_forexfactory_high_impact_event():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    event, values = normalize_forexfactory_event_row(FF_ROW_HIGH_IMPACT)
    assert event["category"] == "economic"
    assert event["market"] == "global"
    assert event["country"] == "US"
    assert event["currency"] == "USD"
    assert event["title"] == "Core CPI m/m"
    assert event["event_date"] == date(2026, 5, 13)
    assert event["release_time_utc"] == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    assert event["source_timezone"] == "America/New_York"
    assert event["importance"] == 3
    assert event["status"] == "released"
    assert event["source"] == "forexfactory"
    assert event["source_event_id"] == "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z"

    by_metric = {v["metric_name"]: v for v in values}
    assert by_metric["actual"]["actual"] is not None
    assert by_metric["actual"]["forecast"] is not None
    assert by_metric["actual"]["previous"] is not None
    assert by_metric["actual"]["unit"] == "%"


@pytest.mark.unit
def test_normalize_forexfactory_scheduled_when_no_actual():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    row = {**FF_ROW_HIGH_IMPACT, "actual": None}
    event, _ = normalize_forexfactory_event_row(row)
    assert event["status"] == "scheduled"


@pytest.mark.unit
def test_normalize_forexfactory_low_medium_high_to_int_importance():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    for raw, expected in [("low", 1), ("medium", 2), ("high", 3), ("holiday", None)]:
        row = {**FF_ROW_HIGH_IMPACT, "impact": raw}
        event, _ = normalize_forexfactory_event_row(row)
        assert event["importance"] == expected, raw


@pytest.mark.unit
def test_normalize_forexfactory_strips_value_suffixes_to_decimal():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    row = {
        **FF_ROW_HIGH_IMPACT,
        "actual": "1.25%",
        "forecast": "1.30%",
        "previous": "1.10%",
    }
    _, values = normalize_forexfactory_event_row(row)
    by_metric = {v["metric_name"]: v for v in values}
    val = by_metric["actual"]
    assert val["actual"] == Decimal("1.25")
    assert val["forecast"] == Decimal("1.30")
    assert val["previous"] == Decimal("1.10")
    assert val["unit"] == "%"


@pytest.mark.unit
def test_normalize_forexfactory_emits_no_values_when_all_blank():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    row = {**FF_ROW_HIGH_IMPACT, "actual": None, "forecast": None, "previous": None}
    _, values = normalize_forexfactory_event_row(row)
    assert values == []


@pytest.mark.unit
def test_normalize_forexfactory_requires_title_and_date():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    bad = {**FF_ROW_HIGH_IMPACT, "title": ""}
    with pytest.raises(ValueError):
        normalize_forexfactory_event_row(bad)
