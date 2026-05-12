from __future__ import annotations

import datetime as dt
from contextlib import AbstractAsyncContextManager

import pytest
import sqlalchemy as sa

from app.jobs import investor_flow_snapshots as job
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.investor_flow_snapshots.builder import InvestorFlowBuildResult
from app.services.investor_flow_snapshots.repository import InvestorFlowSnapshotUpsert


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


def _payload(
    symbol: str, snapshot_date: dt.date = dt.date(2026, 5, 12)
) -> InvestorFlowSnapshotUpsert:
    return InvestorFlowSnapshotUpsert(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        foreign_net=100,
        institution_net=50,
        individual_net=-150,
        source="naver_finance",
        collected_at=dt.datetime(2026, 5, 12, 7, 0, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_dry_run_reports_counts_and_idempotency_without_writing(
    bind_job_session, db_session, monkeypatch
):
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900311", "900312"])
        )
    )
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.symbol.in_(["900311", "900312"])
        )
    )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900311", name="ROB205 A", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900312", name="ROB205 B", exchange="KOSPI", is_active=True
            ),
        ]
    )
    await db_session.commit()

    async def fake_builder(**kwargs):
        return InvestorFlowBuildResult(
            payloads=[_payload(symbol) for symbol in kwargs["symbols"]],
            warnings=("fixture warning",),
        )

    monkeypatch.setattr(job, "build_investor_flow_snapshots", fake_builder)

    result = await job.run_investor_flow_snapshot_build(
        job.InvestorFlowSnapshotBuildRequest(limit=2, commit=False)
    )

    assert result.committed is False
    assert result.symbols_resolved == 2
    assert result.snapshots_built == 2
    assert result.snapshot_date_distribution == {"2026-05-12": 2}
    assert result.idempotency == {
        "wouldInsert": 2,
        "wouldUpdate": 0,
        "duplicatePayloadKeys": 0,
    }
    assert len(result.samples) == 2
    assert result.warnings == ("batch 1: fixture warning",)
    rows = await db_session.execute(
        sa.select(sa.func.count())
        .select_from(InvestorFlowSnapshot)
        .where(InvestorFlowSnapshot.symbol.in_(["900311", "900312"]))
    )
    assert rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_commit_persists_and_second_dry_run_reports_would_update(
    bind_job_session, db_session, monkeypatch
):
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.symbol == "900313")
    )
    await db_session.commit()

    async def fake_builder(**kwargs):
        return InvestorFlowBuildResult(payloads=[_payload(kwargs["symbols"][0])])

    monkeypatch.setattr(job, "build_investor_flow_snapshots", fake_builder)
    request = job.InvestorFlowSnapshotBuildRequest(symbols=("900313",), commit=True)

    committed = await job.run_investor_flow_snapshot_build(request)
    assert committed.committed is True
    assert committed.idempotency["wouldInsert"] == 1

    dry_run = await job.run_investor_flow_snapshot_build(
        job.InvestorFlowSnapshotBuildRequest(symbols=("900313",), commit=False)
    )
    assert dry_run.idempotency == {
        "wouldInsert": 0,
        "wouldUpdate": 1,
        "duplicatePayloadKeys": 0,
    }


@pytest.mark.asyncio
async def test_non_kr_market_rejected(bind_job_session):
    with pytest.raises(ValueError, match="Unsupported investor-flow snapshot market"):
        await job.run_investor_flow_snapshot_build(
            job.InvestorFlowSnapshotBuildRequest(market="us")
        )
