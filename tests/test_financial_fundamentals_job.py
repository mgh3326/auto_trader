from __future__ import annotations

import datetime as dt
from contextlib import AbstractAsyncContextManager

import pandas as pd
import pytest

from app.jobs import financial_fundamentals_snapshots as job
from app.services.financial_fundamentals_snapshots.builder import (
    RawAnnualFiling,
    RawFundamentalsBundle,
)


class _SessionFactory(AbstractAsyncContextManager):
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def bind_job_session(monkeypatch, db_session):
    monkeypatch.setattr(job, "AsyncSessionLocal", lambda: _SessionFactory(db_session))
    return db_session


async def _fake_fetcher(
    symbol: str, *, include_quarterly: bool
) -> RawFundamentalsBundle:
    df = pd.DataFrame(
        [
            {
                "account_id": "ifrs-full_Revenue",
                "account_nm": "매출액",
                "sj_div": "IS",
                "thstrm_amount": "1,000",
            },
            {
                "account_id": "ifrs-full_ProfitLoss",
                "account_nm": "당기순이익",
                "sj_div": "CIS",
                "thstrm_amount": "100",
            },
        ]
    )
    return RawFundamentalsBundle(
        symbol=symbol,
        annual=(RawAnnualFiling(bsns_year=2024, rcept_no="r1", income_statement=df),),
        filing_dates={"r1": dt.date(2025, 3, 20)},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_builds_but_writes_nothing(bind_job_session, monkeypatch):
    monkeypatch.setattr(job, "resolve_symbols", _async_return(["005930"]))

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=False
        ),
        fetcher=_fake_fetcher,
    )
    assert result.committed is False
    assert result.snapshots_built == 1
    assert result.symbols_resolved == 1
    assert any(s.fiscal_period == "2024A" for s in result.samples)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_logs_projected_request_estimate(
    bind_job_session, monkeypatch, caplog
):
    import logging

    monkeypatch.setattr(job, "resolve_symbols", _async_return(["005930"]))
    with caplog.at_level(
        logging.INFO, logger="app.jobs.financial_fundamentals_snapshots"
    ):
        await job.run_financial_fundamentals_snapshot_build(
            job.FinancialFundamentalsSnapshotBuildRequest(
                market="kr",
                symbols=("005930",),
                commit=False,
                include_quarterly=True,
            ),
            fetcher=_fake_fetcher,
        )
    msgs = [r.getMessage() for r in caplog.records]
    # 1 symbol * 41 (quarterly multiplier) — pins the estimate formula.
    assert any("Projected DART requests" in m and "41" in m for m in msgs)


@pytest.mark.asyncio
async def test_job_budget_exceeded_fail_stops_and_does_not_commit(
    bind_job_session, monkeypatch
):
    from decimal import Decimal

    from app.services.financial_fundamentals_snapshots.builder import (
        DartDailyRequestBudgetExceeded,
        FinancialFundamentalsUpsert,
    )

    dummy_payload = FinancialFundamentalsUpsert(
        market="kr",
        symbol="005930",
        fiscal_period="2024A",
        period_type="annual",
        period_end_date=dt.date(2024, 12, 31),
        filing_date=dt.date(2025, 3, 20),
        effective_at=dt.date(2025, 3, 20),
        source="dart",
        source_collected_at=dt.datetime.now(dt.UTC),
        currency="KRW",
        revenue=Decimal("1000"),
        net_income=Decimal("100"),
        gross_profit=None,
        cost_of_sales=None,
        payout_ratio=None,
        dividend_per_share=None,
        discrete_revenue=Decimal("1000"),
        discrete_net_income=Decimal("100"),
        data_state="fresh",
        raw_payload=None,
    )

    async def mock_build(*args, **kwargs):
        raise DartDailyRequestBudgetExceeded(
            "Budget Exceeded", payloads=(dummy_payload,), warnings=("Budget limit hit",)
        )

    monkeypatch.setattr(job, "resolve_symbols", _async_return(["005930"]))
    monkeypatch.setattr(job, "build_financial_fundamentals_for_symbols", mock_build)

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", symbols=("005930",), commit=True
        ),
        fetcher=_fake_fetcher,
    )

    assert result.committed is False
    assert result.snapshots_built == 1
    assert any(s.fiscal_period == "2024A" for s in result.samples)
    assert any(
        "Budget Exceeded" in w or "Budget limit hit" in w for w in result.warnings
    )


def _async_return(value):
    async def _coro(*args, **kwargs):
        return value

    return _coro


@pytest.mark.asyncio
async def test_estimate_only_does_not_fetch_or_commit():
    # No bind_job_session fixture on purpose: estimate-only must short-circuit
    # BEFORE any AsyncSessionLocal use, so this test proves it never touches DB.
    calls: list[str] = []

    async def _spy_fetcher(symbol: str, *, include_quarterly: bool):
        calls.append(symbol)
        raise AssertionError("fetcher must not be called in estimate-only mode")

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr",
            symbols=("005930",),
            estimate_only=True,
            include_quarterly=False,
        ),
        fetcher=_spy_fetcher,
    )
    assert calls == []
    assert result.projected_requests == 11  # 1 symbol * 11 (annual-only)
    assert result.committed is False
    assert result.snapshots_built == 0
    assert any("estimate-only" in w for w in result.warnings)
