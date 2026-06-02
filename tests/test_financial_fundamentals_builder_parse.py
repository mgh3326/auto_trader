from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

from app.services.financial_fundamentals_snapshots.builder import (
    parse_dividend_frame,
    parse_filing_dates_frame,
    parse_income_statement_frame,
    single_quarter_discrete,
)


def test_parse_income_statement_prefers_account_id_then_name():
    df = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "수익(매출액)",
                "sj_div": "IS",
                "thstrm_amount": "3,000,000",
            },
            {
                "account_id": "ifrs-full_GrossProfit",
                "account_nm": "매출총이익",
                "sj_div": "IS",
                "thstrm_amount": "1,200,000",
            },
            {
                "account_id": "ifrs-full_CostOfSales",
                "account_nm": "매출원가",
                "sj_div": "IS",
                "thstrm_amount": "1,800,000",
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": "500,000",
            },
        ]
    )
    parsed = parse_income_statement_frame(df)
    assert parsed["revenue"] == Decimal("3000000")
    assert parsed["gross_profit"] == Decimal("1200000")
    assert parsed["cost_of_sales"] == Decimal("1800000")
    assert parsed["net_income"] == Decimal("500000")


def test_parse_income_statement_missing_gross_profit_is_none():
    df = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": "100",
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": "10",
            },
        ]
    )
    parsed = parse_income_statement_frame(df)
    assert parsed["gross_profit"] is None
    assert parsed["cost_of_sales"] is None
    assert parsed["revenue"] == Decimal("100")


def test_parse_dividend_matches_labels_by_normalized_contains():
    df = pd.DataFrame(
        [
            {"se": "주당 현금배당금(원)", "thstrm": "1,444"},
            {"se": "(연결)현금배당성향(%)", "thstrm": "25.10"},
        ]
    )
    parsed = parse_dividend_frame(df)
    assert parsed["dividend_per_share"] == Decimal("1444")
    assert parsed["payout_ratio"] == Decimal("25.10")


def test_parse_dividend_missing_rows_are_none_not_zero():
    df = pd.DataFrame([{"se": "주식의 종류", "thstrm": "보통주"}])
    parsed = parse_dividend_frame(df)
    assert parsed["dividend_per_share"] is None
    assert parsed["payout_ratio"] is None


def test_parse_filing_dates_maps_rcept_no_to_date():
    df = pd.DataFrame(
        [
            {"rcept_no": "20260320000123", "rcept_dt": "20260320"},
            {"rcept_no": "20250318000077", "rcept_dt": "20250318"},
        ]
    )
    mapping = parse_filing_dates_frame(df)
    assert mapping["20260320000123"] == dt.date(2026, 3, 20)
    assert mapping["20250318000077"] == dt.date(2025, 3, 18)


def test_single_quarter_discrete_differences_cumulative():
    # Q3 cumulative (9-month) minus H1 cumulative (6-month) = standalone Q3.
    assert single_quarter_discrete(
        cumulative=Decimal("900"), prior_cumulative=Decimal("600")
    ) == Decimal("300")
    # Q1 has no prior cumulative within the year → standalone = cumulative.
    assert single_quarter_discrete(
        cumulative=Decimal("250"), prior_cumulative=None
    ) == Decimal("250")
    # Missing cumulative → cannot difference.
    assert (
        single_quarter_discrete(cumulative=None, prior_cumulative=Decimal("600"))
        is None
    )
