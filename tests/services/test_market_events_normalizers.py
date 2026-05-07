"""Pure-function normalizer tests (ROB-128)."""

from __future__ import annotations

from datetime import date
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
    "report_nm": "분기보고서 (2026.03)",
}

DART_ROW_OTHER = {
    "rcept_no": "20260507000456",
    "rcept_dt": "20260507",
    "corp_name": "현대차",
    "corp_code": "00164742",
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
    assert event["symbol"] == "00126380"
    assert event["company_name"] == "삼성전자"
    assert "rcpNo=20260507000123" in event["source_url"]
    assert event["event_date"] == date(2026, 5, 7)
    assert values == []


@pytest.mark.unit
def test_normalize_dart_unrelated_filing_classifies_as_disclosure():
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    event, _ = normalize_dart_disclosure_row(DART_ROW_OTHER)
    assert event["category"] == "disclosure"
