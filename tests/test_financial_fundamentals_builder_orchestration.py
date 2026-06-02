from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
    build_financial_fundamentals_for_symbols,
)


def _is_frame(rev: str, ni: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"account_id": "ifrs-full_Revenue", "account_nm": "매출액", "sj_div": "IS", "thstrm_amount": rev},
            {"account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익", "sj_div": "CIS", "thstrm_amount": ni},
        ]
    )


def _div_frame(dps: str, payout: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"se": "주당 현금배당금(원)", "thstrm": dps},
            {"se": "현금배당성향(%)", "thstrm": payout},
        ]
    )


async def _fake_fetcher(symbol: str, *, include_quarterly: bool) -> RawFundamentalsBundle:
    return RawFundamentalsBundle(
        symbol=symbol,
        currency="KRW",
        annual=(
            RawAnnualFiling(
                bsns_year=2024,
                rcept_no="20250318000077",
                income_statement=_is_frame("2,000,000", "200,000"),
                dividend=_div_frame("1,000", "20.0"),
            ),
            RawAnnualFiling(
                bsns_year=2025,
                rcept_no="20260320000123",
                income_statement=_is_frame("3,000,000", "300,000"),
                dividend=_div_frame("1,444", "25.10"),
            ),
        ),
        quarterly=(),
        filing_dates={
            "20250318000077": dt.date(2025, 3, 18),
            "20260320000123": dt.date(2026, 3, 20),
        },
    )


@pytest.mark.asyncio
async def test_builder_emits_one_payload_per_annual_period_with_pit_filing_date():
    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
        fetcher=_fake_fetcher,
    )
    payloads = {p.fiscal_period: p for p in result.payloads}
    assert set(payloads) == {"2024A", "2025A"}
    p25 = payloads["2025A"]
    assert p25.market == "kr" and p25.symbol == "005930" and p25.source == "dart"
    assert p25.period_type == "annual"
    assert p25.period_end_date == dt.date(2025, 12, 31)
    assert p25.filing_date == dt.date(2026, 3, 20)        # rcept_no→rcept_dt join
    assert p25.effective_at == dt.date(2026, 3, 20)
    assert p25.revenue == Decimal("3000000")
    assert p25.net_income == Decimal("300000")
    assert p25.payout_ratio == Decimal("25.10")
    assert p25.dividend_per_share == Decimal("1444")
    assert p25.data_state == "fresh"                      # filing_date resolved
    assert p25.raw_payload is not None                    # provenance retained


@pytest.mark.asyncio
async def test_builder_marks_partial_when_filing_date_unresolved():
    async def _fetcher(symbol: str, *, include_quarterly: bool) -> RawFundamentalsBundle:
        bundle = await _fake_fetcher(symbol, include_quarterly=include_quarterly)
        return RawFundamentalsBundle(
            symbol=bundle.symbol,
            currency=bundle.currency,
            annual=bundle.annual,
            quarterly=bundle.quarterly,
            filing_dates={},  # join fails for every rcept_no
        )

    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
        fetcher=_fetcher,
    )
    assert all(p.filing_date is None for p in result.payloads)
    assert all(p.data_state == "partial" for p in result.payloads)
