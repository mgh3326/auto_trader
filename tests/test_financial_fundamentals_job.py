from __future__ import annotations

import datetime as dt
from contextlib import AbstractAsyncContextManager

import pandas as pd
import pytest
import sqlalchemy as sa

from app.jobs import financial_fundamentals_snapshots as job
from app.models.kr_symbol_universe import KRSymbolUniverse
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


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("005930", "삼성전자", True),
        ("035420", "NAVER", True),
        ("000087", "하이트진로2우B", False),
        ("005935", "삼성전자우", False),
        ("069500", "KODEX 200", False),
        ("0000H0", "KODEX 인도Nifty미드캡100", False),
        ("0004Y0", "디비금융제14호스팩", False),
        ("123456", "맥쿼리인프라", False),
    ],
)
def test_dart_common_kr_equity_filter_excludes_non_common(symbol, name, expected):
    assert job.is_dart_common_kr_equity(symbol, name) is expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_symbols_filters_common_kr_equities_before_limit(
    bind_job_session, db_session
):
    symbols = ["0000H0", "000001", "000002", "000003", "000004", "000005"]
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(symbols))
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="0000H0",
                name="KODEX 인도Nifty미드캡100",
                exchange="KOSPI",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol="000001",
                name="테스트보통주A",
                exchange="KOSPI",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol="000002",
                name="테스트보통주A우",
                exchange="KOSPI",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol="000003",
                name="TIGER 테스트ETF",
                exchange="KOSPI",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol="000004",
                name="테스트제1호스팩",
                exchange="KOSDAQ",
                is_active=True,
            ),
            KRSymbolUniverse(
                symbol="000005",
                name="테스트보통주B",
                exchange="KOSDAQ",
                is_active=True,
            ),
        ]
    )
    await db_session.commit()
    try:
        assert await job.resolve_symbols("kr", [], 2) == ["000001", "000005"]
    finally:
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(symbols))
        )
        await db_session.commit()


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
            market="kr", symbols=("005930",), commit=True, allow_partial=True
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skip_existing_budget_split(bind_job_session, db_session, monkeypatch):
    # ROB-441 budget-split: --skip-existing drops already-collected symbols so daily
    # re-runs advance through the uncollected universe within the DART daily budget.
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import (
        FinancialFundamentalsSnapshot,
    )

    syms = ["900001", "900002", "900003"]
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    # 900001 already collected → must be skipped.
    db_session.add(
        FinancialFundamentalsSnapshot(
            market="kr",
            symbol="900001",
            fiscal_period="2024A",
            period_type="annual",
            period_end_date=dt.date(2024, 12, 31),
            source="dart",
            source_collected_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            data_state="fresh",
        )
    )
    await db_session.commit()

    async def _fake_universe(market):  # noqa: ANN001
        return list(syms)

    monkeypatch.setattr(job, "resolve_active_universe", _fake_universe)

    result = await job.run_financial_fundamentals_snapshot_build(
        job.FinancialFundamentalsSnapshotBuildRequest(
            market="kr", all_symbols=True, estimate_only=True, skip_existing=True
        )
    )
    assert result.symbols_resolved == 2  # 900001 skipped (already collected)
    assert result.projected_requests == 2 * 11  # only uncollected projected
    assert any(
        "skip_existing" in w and "1 already-collected" in w for w in result.warnings
    )

    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.commit()
