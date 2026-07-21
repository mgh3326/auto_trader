from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.jobs import support_proximity_snapshots as job
from app.services.invest_screener_snapshots.repository import SnapshotUpsert
from app.services.invest_screener_snapshots.support_proximity_builder import (
    SupportProximityBuildBatch,
    SupportProximityCandidate,
)
from app.services.market_valuation_snapshots.normalized_market_cap import (
    NormalizedMarketCap,
)


class _Session:
    def __init__(self) -> None:
        self.commit = AsyncMock()


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _batch() -> SupportProximityBuildBatch:
    cap = NormalizedMarketCap(
        value=Decimal("60300000000000"),
        snapshot_date=dt.date(2026, 7, 20),
        source="naver_finance",
    )
    candidate = SupportProximityCandidate(
        symbol="005930", market_cap=cap, proxy_distance_pct=1.0
    )
    payload = SnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 7, 20),
        latest_close=Decimal("50000"),
        closes_window=[49000.0, 50000.0],
        daily_volume=1_000_000,
        daily_turnover=Decimal("50000000000"),
        market_cap=cap.value,
        market_cap_source=cap.source,
        market_cap_snapshot_date=cap.snapshot_date,
        support_price=Decimal("49000"),
        support_kind="bb_lower",
        support_strength="strong",
        dist_to_support_pct=Decimal("2.0000"),
        support_computed_at=dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.UTC),
        source="kis",
    )
    return SupportProximityBuildBatch(
        source_partition_date=dt.date(2026, 7, 20),
        candidates=(candidate,),
        payloads=(payload,),
    )


@pytest.mark.asyncio
async def test_job_is_dry_run_by_default(monkeypatch):
    session = _Session()
    monkeypatch.setattr(
        job, "build_support_proximity_snapshots", AsyncMock(return_value=_batch())
    )

    class _Repository:
        upsert = AsyncMock()

        def __init__(self, _session):
            pass

    monkeypatch.setattr(job, "InvestScreenerSnapshotsRepository", _Repository)

    result = await job.run_support_proximity_build(
        job.SupportProximityBuildRequest(),
        session_factory=lambda: _SessionContext(session),
    )

    assert result.committed is False
    assert result.snapshots_built == 1
    assert result.supports_built == 1
    _Repository.upsert.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_uses_repository_upsert_only(monkeypatch):
    session = _Session()
    batch = _batch()
    monkeypatch.setattr(
        job, "build_support_proximity_snapshots", AsyncMock(return_value=batch)
    )
    upsert = AsyncMock()

    class _Repository:
        def __init__(self, received_session):
            assert received_session is session

        async def upsert(self, payload):
            await upsert(payload)

    monkeypatch.setattr(job, "InvestScreenerSnapshotsRepository", _Repository)

    result = await job.run_support_proximity_build(
        job.SupportProximityBuildRequest(commit=True),
        session_factory=lambda: _SessionContext(session),
    )

    assert result.committed is True
    upsert.assert_awaited_once_with(batch.payloads[0])
    session.commit.assert_awaited_once()


def test_manual_task_has_no_schedule():
    from app.tasks import invest_screener_snapshot_tasks

    task = invest_screener_snapshot_tasks.build_support_proximity_snapshots
    assert (getattr(task, "labels", {}) or {}).get("schedule") is None


@pytest.mark.asyncio
async def test_manual_task_defaults_to_dry_run_and_returns_summary(monkeypatch):
    from app.tasks import invest_screener_snapshot_tasks as task_module

    async def _fake_run(request):
        assert request.commit is False
        assert request.candidate_pool_limit == 10
        return job.SupportProximityBuildResult(
            market="kr",
            source_partition_date=dt.date(2026, 7, 20),
            candidates_resolved=1,
            snapshots_built=1,
            supports_built=1,
            skipped=0,
            committed=False,
            started_at=dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 7, 20, 10, 1, tzinfo=dt.UTC),
            samples=job._sample_rows(_batch().payloads),
        )

    monkeypatch.setattr(task_module, "run_support_proximity_build", _fake_run)
    task = task_module.build_support_proximity_snapshots
    raw_func = getattr(task, "original_func", task)

    result = await raw_func(candidate_pool_limit=10)

    assert result["committed"] is False
    assert result["sourcePartitionDate"] == "2026-07-20"
    assert result["supportsBuilt"] == 1
    assert result["samples"][0]["supportPrice"] == "49000"
    assert result["samples"][0]["supportComputedAt"] == "2026-07-20T10:00:00+00:00"
