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
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": rev,
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": ni,
            },
        ]
    )


def _div_frame(dps: str, payout: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"se": "주당 현금배당금(원)", "thstrm": dps},
            {"se": "현금배당성향(%)", "thstrm": payout},
        ]
    )


async def _fake_fetcher(
    symbol: str, *, include_quarterly: bool
) -> RawFundamentalsBundle:
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
    assert p25.filing_date == dt.date(2026, 3, 20)  # rcept_no→rcept_dt join
    assert p25.effective_at == dt.date(2026, 3, 20)
    assert p25.revenue == Decimal("3000000")
    assert p25.net_income == Decimal("300000")
    assert p25.payout_ratio == Decimal("25.10")
    assert p25.dividend_per_share == Decimal("1444")
    assert p25.data_state == "fresh"  # filing_date resolved
    assert p25.raw_payload is not None  # provenance retained


@pytest.mark.asyncio
async def test_builder_marks_partial_when_filing_date_unresolved():
    async def _fetcher(
        symbol: str, *, include_quarterly: bool
    ) -> RawFundamentalsBundle:
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


class FakeOpenDartClient:
    def __init__(self):
        self.calls = []

    def finstate_all(self, symbol, year, reprt_code, fs_div="CFS"):
        self.calls.append(("finstate_all", symbol, year, reprt_code, fs_div))
        return pd.DataFrame(
            [
                {
                    "account_id": "ifrs-full_Revenue",
                    "account_nm": "매출액",
                    "sj_div": "IS",
                    "thstrm_amount": "1,000,000",
                    "rcept_no": f"rcept_{year}_{reprt_code}",
                    "currency": "KRW",
                }
            ]
        )

    def report(self, symbol, report_type, year, reprt_code):
        self.calls.append(("report", symbol, report_type, year, reprt_code))
        return pd.DataFrame(
            [
                {"se": "주당 현금배당금(원)", "thstrm": "500"},
                {"se": "현금배당성향(%)", "thstrm": "10.0"},
            ]
        )

    def list(self, corp, start, end, kind="A", final=True):
        self.calls.append(("list", corp, start, end, kind, final))
        return pd.DataFrame(
            [
                {"rcept_no": "rcept_2025_11011", "rcept_dt": "20260320"},
                {"rcept_no": "rcept_2025_11013", "rcept_dt": "20250515"},
                {"rcept_no": "rcept_2025_11012", "rcept_dt": "20250814"},
                {"rcept_no": "rcept_2025_11014", "rcept_dt": "20251114"},
            ]
        )


@pytest.mark.asyncio
async def test_default_dart_fetcher_quarterly(monkeypatch):
    fake_client = FakeOpenDartClient()

    async def mock_get_client():
        return fake_client

    monkeypatch.setattr("app.services.disclosures.dart._get_client", mock_get_client)
    monkeypatch.setattr("app.core.config.settings.opendart_api_key", "dummy")

    from app.services.financial_fundamentals_snapshots.builder import (
        default_dart_fetcher,
    )

    bundle = await default_dart_fetcher("005930", include_quarterly=True, years_back=1)

    assert bundle.symbol == "005930"
    assert bundle.currency == "KRW"
    assert len(bundle.annual) == 1

    assert len(bundle.quarterly) == 4
    q_by_num = {q.quarter: q for q in bundle.quarterly}
    assert set(q_by_num.keys()) == {1, 2, 3, 4}
    assert q_by_num[1].prior_income_statement is None
    assert q_by_num[2].prior_income_statement is not None
    assert q_by_num[3].prior_income_statement is not None
    assert q_by_num[4].prior_income_statement is not None

    # Q4 reuses the annual FY filing — it must NOT issue an extra finstate_all
    # for reprt_code 11011 beyond the single annual fetch (budget boundary).
    calls_11011 = [
        c for c in fake_client.calls if c[0] == "finstate_all" and c[3] == "11011"
    ]
    assert len(calls_11011) == 1


@pytest.mark.asyncio
async def test_default_dart_fetcher_annual_only(monkeypatch):
    fake_client = FakeOpenDartClient()

    async def mock_get_client():
        return fake_client

    monkeypatch.setattr("app.services.disclosures.dart._get_client", mock_get_client)
    monkeypatch.setattr("app.core.config.settings.opendart_api_key", "dummy")

    from app.services.financial_fundamentals_snapshots.builder import (
        default_dart_fetcher,
    )

    bundle = await default_dart_fetcher("005930", include_quarterly=False, years_back=1)
    assert len(bundle.annual) == 1
    assert bundle.quarterly == ()


@pytest.mark.asyncio
async def test_budget_exceeded_raises_and_stops(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.opendart_daily_request_budget", 2)
    monkeypatch.setattr("app.core.config.settings.opendart_api_key", "dummy")

    fake_client = FakeOpenDartClient()

    async def mock_get_client():
        return fake_client

    monkeypatch.setattr("app.services.disclosures.dart._get_client", mock_get_client)

    from app.services.financial_fundamentals_snapshots.builder import (
        DartDailyRequestBudgetExceeded,
        default_dart_fetcher,
        reset_request_count,
    )

    reset_request_count()

    with pytest.raises(DartDailyRequestBudgetExceeded):
        await default_dart_fetcher("005930", include_quarterly=False, years_back=2)


@pytest.mark.asyncio
async def test_quarterly_discrete_differencing_end_to_end():
    """YTD-cumulative quarters -> standalone discrete = cumulative - prior,
    asserted through _payload_from_quarterly (spec A4 row 2 / A5.1)."""
    from app.services.financial_fundamentals_snapshots.builder import RawQuarterlyFiling

    q1 = RawQuarterlyFiling(
        bsns_year=2025,
        quarter=1,
        rcept_no="r1",
        reprt_code="11013",
        income_statement=_is_frame("1000", "100"),
        prior_income_statement=None,
    )
    q2 = RawQuarterlyFiling(
        bsns_year=2025,
        quarter=2,
        rcept_no="r2",
        reprt_code="11012",
        income_statement=_is_frame("2500", "250"),
        prior_income_statement=_is_frame("1000", "100"),
    )
    q3 = RawQuarterlyFiling(
        bsns_year=2025,
        quarter=3,
        rcept_no="r3",
        reprt_code="11014",
        income_statement=_is_frame("4500", "450"),
        prior_income_statement=_is_frame("2500", "250"),
    )

    async def _fetcher(symbol, include_quarterly=False):
        return RawFundamentalsBundle(
            symbol=symbol,
            currency="KRW",
            annual=(),
            quarterly=(q1, q2, q3),
            filing_dates={},
        )

    result = await build_financial_fundamentals_for_symbols(
        market="kr",
        symbols=["005930"],
        collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
        fetcher=_fetcher,
        include_quarterly=True,
    )
    q = {p.fiscal_period: p for p in result.payloads if p.period_type == "quarterly"}
    assert q["2025Q1"].discrete_net_income == Decimal("100")  # Q1 standalone
    assert q["2025Q2"].discrete_net_income == Decimal("150")  # 250 - 100
    assert q["2025Q3"].discrete_net_income == Decimal("200")  # 450 - 250


@pytest.mark.asyncio
async def test_quarterly_skips_quarter_when_prior_missing(monkeypatch):
    """A missing intermediate quarter must NOT fake the next quarter's discrete
    (YTD cumulative mislabeled as standalone). The undifferenced quarter is
    skipped so QoQ later fails closed (fail-soft, ROB-425 impl-review major)."""

    class FakeMissingHalfYear:
        def __init__(self):
            self.calls = []

        def finstate_all(self, symbol, year, reprt_code, fs_div="CFS"):
            self.calls.append(("finstate_all", symbol, year, reprt_code, fs_div))
            if reprt_code == "11012":  # 반기(Q2 cumulative) absent
                return pd.DataFrame()
            return _is_frame("1000", "100")

        def report(self, *args, **kwargs):
            return None

        def list(self, corp, start, end, kind="A", final=True):
            return pd.DataFrame([{"rcept_no": "x", "rcept_dt": "20260320"}])

    fake = FakeMissingHalfYear()

    async def mock_get_client():
        return fake

    monkeypatch.setattr("app.services.disclosures.dart._get_client", mock_get_client)
    monkeypatch.setattr("app.core.config.settings.opendart_api_key", "dummy")

    from app.services.financial_fundamentals_snapshots.builder import (
        default_dart_fetcher,
        reset_request_count,
    )

    reset_request_count()
    bundle = await default_dart_fetcher("005930", include_quarterly=True, years_back=1)
    quarters = {q.quarter for q in bundle.quarterly}
    assert 2 not in quarters  # Q2 frame empty -> not collected
    assert 3 not in quarters  # Q3 prior (Q2) missing -> skipped, NOT faked
    assert 1 in quarters  # Q1 standalone (prior None) still emitted
    assert 4 in quarters  # Q4 prior (Q3) present -> correctly differenced
