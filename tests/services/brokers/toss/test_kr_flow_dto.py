from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.brokers.toss.dto import (
    parse_credit_trades_page,
    parse_investor_trading_rankings,
    parse_program_trades_page,
    parse_securities_lending_page,
    parse_short_selling_page,
)
from app.services.brokers.toss.errors import TossResponseContractError

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "kr_flow"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    ("name", "parser", "field", "expected"),
    [
        ("toss_short_selling.json", parse_short_selling_page, "shortSellingAmount", Decimal("41250000000")),
        ("toss_securities_lending.json", parse_securities_lending_page, "balanceAmount", Decimal("1218720000000")),
        ("toss_credit_trades.json", parse_credit_trades_page, "marginLoan", Decimal("125300")),
        ("toss_program_trades.json", parse_program_trades_page, "arbitrage", Decimal("-31100")),
    ],
)
def test_toss_flow_fixtures_keep_exact_dates_and_decimals(name, parser, field, expected):
    page = parser(fixture(name))
    assert page.records[0]["date"] == date(2026, 7, 16 if "short" in name or "credit" in name else 17)
    value = page.records[0][field]
    if isinstance(value, dict):
        value = value["newQuantity" if field == "marginLoan" else "netBuyVolume"]
    assert value == expected


@pytest.mark.parametrize(
    ("name", "parser", "field"),
    [
        ("toss_short_selling.json", parse_short_selling_page, "shortSellingAmount"),
        ("toss_securities_lending.json", parse_securities_lending_page, "balanceAmount"),
        ("toss_credit_trades.json", parse_credit_trades_page, "marginLoan"),
        ("toss_program_trades.json", parse_program_trades_page, "arbitrage"),
    ],
)
def test_toss_flow_malformed_decimal_is_typed_error(name, parser, field):
    raw = fixture(name)
    raw["records"][0][field] = {"newQuantity": "bad"} if field == "marginLoan" else "bad"
    with pytest.raises(TossResponseContractError):
        parser(raw)


def test_toss_rankings_parse_exact_amount_and_asof():
    rankings = parse_investor_trading_rankings(fixture("toss_investor_rankings.json"))
    assert rankings["foreigner"].buy[0]["amount"] == Decimal("386487682000")
    assert rankings["foreigner"].ranked_at.isoformat() == "2026-10-20T15:41:19+09:00"
    assert rankings["institution"].ranked_at is None


@pytest.mark.parametrize("field", ["marginLoan", "stockLoan"])
@pytest.mark.parametrize("value", [None, "missing"])
def test_credit_trade_nullable_details_are_preserved(field, value):
    raw = fixture("toss_credit_trades.json")
    if value == "missing":
        del raw["records"][0][field]
    else:
        raw["records"][0][field] = value
    page = parse_credit_trades_page(raw)
    assert page.records[0][field] is None
