import pytest
from pydantic import ValidationError

from app.schemas.investment_dimension_reports import (
    HermesDimensionReport,
    HermesDimensionReportsIngestRequest,
)


def test_dimension_report_rejects_unknown_dimension():
    with pytest.raises(ValidationError):
        HermesDimensionReport(dimension="macro", report_text="x")


def test_dimension_report_accepts_market_wide_null_symbol():
    r = HermesDimensionReport(
        dimension="market", report_text="시장 개요", stance="bullish",
        confidence=70, key_findings=["상승 우위"], signals={"breadth": "60% adv"},
    )
    assert r.symbol is None and r.stance == "bullish"


def test_ingest_request_forbids_extra():
    with pytest.raises(ValidationError):
        HermesDimensionReportsIngestRequest(
            run_envelope={"run_uuid": "x"}, dimension_reports=[], bogus=1
        )
