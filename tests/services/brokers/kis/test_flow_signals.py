from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.brokers.kis.flow_signals import (
    KISFlowResponseContractError,
    parse_member_summary,
    parse_vi_status_rows,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "kr_flow"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_kis_vi_fixture_parses_code_date_and_exact_prices():
    row = parse_vi_status_rows(fixture("kis_vi_status.json"), symbol="005930")[0]
    assert row["vi_strt_dt"] == date(2026, 11, 26)
    assert row["vi_prpr"] == Decimal("70200")
    assert row["vi_val"] == Decimal("100000000")


def test_kis_member_fixture_parses_top_brokers():
    parsed = parse_member_summary(fixture("kis_member.json"))
    assert parsed["buyers"][0]["member_name"] == "미래에셋증권"
    assert parsed["buyers"][0]["volume"] == Decimal("150000")
    assert parsed["buyers"][0]["trading_amount"] == Decimal("10500000000")
    assert parsed["sellers"][0]["volume"] == Decimal("120000")


def test_kis_malformed_member_row_is_typed_error():
    raw = fixture("kis_member.json")
    raw["shnu_vol1"] = "bad"
    with pytest.raises(KISFlowResponseContractError):
        parse_member_summary(raw)


def test_kis_vi_row_for_another_symbol_is_typed_error():
    with pytest.raises(KISFlowResponseContractError):
        parse_vi_status_rows(fixture("kis_vi_status.json"), symbol="000660")
