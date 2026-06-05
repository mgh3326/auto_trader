"""ROB-441 PR1: US fundamentals parser (yfinance income → FinancialFundamentalsUpsert)
+ market-agnostic derive reuse. Pure unit tests (no DB)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.services.financial_fundamentals_snapshots.builder_us import (
    build_us_fundamentals_for_symbols,
    fetch_us_annual_fundamentals,
    parse_us_annual_income_periods,
)

_COLLECTED = dt.datetime(2026, 6, 5, tzinfo=dt.UTC)


def _period(rev, ni, gp=None, cos=None) -> dict:
    d: dict = {"Total Revenue": rev, "Net Income": ni}
    if gp is not None:
        d["Gross Profit"] = gp
    if cos is not None:
        d["Cost Of Revenue"] = cos
    return d


@pytest.mark.unit
def test_parse_basic_annual() -> None:
    rows = parse_us_annual_income_periods(
        symbol="aapl",
        data={"2024-12-31": _period(1000, 100, gp=400, cos=600)},
        collected_at=_COLLECTED,
    )
    assert len(rows) == 1
    r = rows[0]
    assert (r.market, r.symbol, r.source) == ("us", "AAPL", "yfinance")
    assert r.fiscal_period == "2024A"
    assert r.period_type == "annual"
    assert r.period_end_date == dt.date(2024, 12, 31)
    # PIT: filing_date = period_end + 90d (no look-ahead)
    assert r.filing_date == dt.date(2024, 12, 31) + dt.timedelta(days=90)
    assert r.revenue == Decimal("1000")
    assert r.net_income == Decimal("100")
    assert r.gross_profit == Decimal("400")
    assert r.cost_of_sales == Decimal("600")
    assert r.discrete_revenue == Decimal("1000")  # annual: discrete == reported
    assert r.currency == "USD"


@pytest.mark.unit
def test_label_matching_alternatives() -> None:
    rows = parse_us_annual_income_periods(
        symbol="X",
        data={
            "2023-09-30": {
                "Operating Revenue": 500,
                "Net Income Common Stockholders": 50,
            }
        },
        collected_at=_COLLECTED,
    )
    assert len(rows) == 1
    assert rows[0].revenue == Decimal("500")
    assert rows[0].net_income == Decimal("50")
    assert rows[0].fiscal_period == "2023A"


@pytest.mark.unit
def test_fail_closed_skips_period_without_revenue_or_income() -> None:
    rows = parse_us_annual_income_periods(
        symbol="X",
        data={"2024-12-31": {"Gross Profit": 400}},  # no revenue/net_income
        collected_at=_COLLECTED,
    )
    assert rows == []


@pytest.mark.unit
def test_skips_bad_date_and_non_finite() -> None:
    rows = parse_us_annual_income_periods(
        symbol="X",
        data={
            "not-a-date": {"Total Revenue": 100, "Net Income": 10},
            "2024-12-31": {"Total Revenue": float("nan"), "Net Income": 10},
        },
        collected_at=_COLLECTED,
    )
    # bad-date skipped; 2024 kept (net_income present, NaN revenue → None)
    assert len(rows) == 1
    assert rows[0].revenue is None
    assert rows[0].net_income == Decimal("10")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_parses_yfinance_payload() -> None:
    async def _fake(symbol, statement, freq):  # noqa: ANN001
        assert statement == "income"
        assert freq == "annual"
        return {"data": {"2024-12-31": _period(1000, 100)}}

    with patch(
        "app.mcp_server.tooling.fundamentals_sources_yfinance._fetch_financials_yfinance",
        _fake,
    ):
        rows = await fetch_us_annual_fundamentals(
            symbol="AAPL", collected_at=_COLLECTED
        )
    assert len(rows) == 1
    assert rows[0].revenue == Decimal("1000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_fail_closed_on_error() -> None:
    async def _boom(symbol, statement, freq):  # noqa: ANN001
        raise RuntimeError("yfinance down")

    with patch(
        "app.mcp_server.tooling.fundamentals_sources_yfinance._fetch_financials_yfinance",
        _boom,
    ):
        rows = await fetch_us_annual_fundamentals(
            symbol="AAPL", collected_at=_COLLECTED
        )
    assert rows == []


@pytest.mark.unit
def test_derive_reuses_us_periods() -> None:
    from app.services.financial_fundamentals_snapshots.derive import (
        FundamentalPeriod,
        derive_fundamentals_metrics,
    )

    rows = parse_us_annual_income_periods(
        symbol="X",
        data={
            "2021-12-31": _period(1000, 100),
            "2022-12-31": _period(1100, 120),
            "2023-12-31": _period(1210, 140),
            "2024-12-31": _period(1331, 160),
        },
        collected_at=_COLLECTED,
    )
    periods = [
        FundamentalPeriod(
            fiscal_period=r.fiscal_period,
            period_type=r.period_type,
            period_end_date=r.period_end_date,
            filing_date=r.filing_date,
            revenue=r.revenue,
            net_income=r.net_income,
            gross_profit=r.gross_profit,
            cost_of_sales=r.cost_of_sales,
            discrete_revenue=r.discrete_revenue,
            discrete_net_income=r.discrete_net_income,
        )
        for r in rows
    ]
    # report_date after all approximated filing dates (2024-12-31 + 90d = 2025-03-31)
    deriv = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    # 4 visible annuals → 3 YoY deltas → 3y-avg growth computed (not unavailable).
    assert deriv.revenue_growth_3y_avg.value is not None
    assert deriv.revenue_growth_3y_avg.state != "unavailable"
    assert deriv.earnings_growth_3y_avg.value is not None


# --- ROB-441 PR2: build orchestration --------------------------------------


async def _fetch_one_period(*, symbol, collected_at):  # noqa: ANN001
    if symbol == "EMPTY":
        return []
    return parse_us_annual_income_periods(
        symbol=symbol,
        data={"2024-12-31": _period(1000, 100)},
        collected_at=collected_at,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_dry_run_no_commit() -> None:
    result = await build_us_fundamentals_for_symbols(
        ["AAPL", "EMPTY"], commit=False, fetcher=_fetch_one_period
    )
    assert result.symbols_resolved == 2
    assert result.snapshots_built == 1  # AAPL 1 period; EMPTY 0
    assert result.committed is False
    assert any("EMPTY" in w for w in result.warnings)
    assert len(result.samples) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_commit_writes(monkeypatch) -> None:
    captured: dict = {}

    class _StubRepo:
        def __init__(self, session):  # noqa: ANN001
            pass

        async def upsert(self, payloads):  # noqa: ANN001
            captured["rows"] = list(payloads)
            return len(captured["rows"])

    class _FakeCM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):  # noqa: ANN002
            return False

    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.financial_fundamentals_snapshots.repository."
        "FinancialFundamentalsSnapshotsRepository",
        _StubRepo,
    )
    result = await build_us_fundamentals_for_symbols(
        ["AAPL"], commit=True, fetcher=_fetch_one_period
    )
    assert result.committed is True
    assert result.snapshots_built == 1
    assert len(captured["rows"]) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_fetch_error_is_fail_closed() -> None:
    async def _boom(*, symbol, collected_at):  # noqa: ANN001
        raise RuntimeError("yfinance down")

    result = await build_us_fundamentals_for_symbols(["X"], commit=True, fetcher=_boom)
    assert result.snapshots_built == 0
    assert result.committed is False  # nothing to commit
    assert any("fetch failed" in w for w in result.warnings)
