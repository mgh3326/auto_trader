import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.jobs import invest_screener_snapshots as snapshot_job
from app.services.invest_screener_snapshots.repository import SnapshotUpsert


@pytest.mark.asyncio
async def test_snapshot_job_dry_run_does_not_commit(monkeypatch):
    payload = SnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 12),
        latest_close=Decimal("78500"),
        closes_window=[78500, 78000, 77000, 76000, 75000],
        consecutive_up_days=3,
        week_change_rate=Decimal("4.66"),
        source="kis",
    )
    monkeypatch.setattr(
        snapshot_job,
        "resolve_symbols",
        AsyncMock(return_value=["005930"]),
    )
    monkeypatch.setattr(
        snapshot_job,
        "build_snapshots_for_market",
        AsyncMock(return_value=[payload]),
    )
    commit_mock = AsyncMock()
    monkeypatch.setattr(snapshot_job, "_commit_payloads", commit_mock)

    result = await snapshot_job.run_snapshot_build(
        snapshot_job.SnapshotBuildRequest(market="kr", limit=1, commit=False)
    )

    assert result.market == "kr"
    assert result.symbols_resolved == 1
    assert result.snapshots_built == 1
    assert result.skipped == 0
    assert result.committed is False
    assert result.batches == 1
    assert result.snapshot_date_distribution == {"2026-05-12": 1}
    assert result.samples[0].symbol == "005930"
    commit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_job_commit_uses_repository_boundary(monkeypatch):
    payload = SnapshotUpsert(
        market="us",
        symbol="AAPL",
        snapshot_date=dt.date(2026, 5, 12),
        latest_close=Decimal("210.12"),
        closes_window=[210.12, 209.01, 208.34, 207.22, 206.11],
        consecutive_up_days=2,
        week_change_rate=Decimal("1.94"),
        source="yahoo",
    )
    monkeypatch.setattr(
        snapshot_job,
        "resolve_active_universe",
        AsyncMock(return_value=["AAPL"]),
    )
    monkeypatch.setattr(
        snapshot_job,
        "build_snapshots_for_market",
        AsyncMock(return_value=[payload]),
    )
    commit_mock = AsyncMock()
    monkeypatch.setattr(snapshot_job, "_commit_payloads", commit_mock)

    result = await snapshot_job.run_snapshot_build(
        snapshot_job.SnapshotBuildRequest(
            market="us", all_symbols=True, batch_size=1, commit=True
        )
    )

    assert result.committed is True
    assert result.snapshots_built == 1
    commit_mock.assert_awaited_once_with([payload])


def test_snapshot_task_is_registered_without_recurring_schedule():
    from app.tasks import TASKIQ_TASK_MODULES, invest_screener_snapshot_tasks

    assert invest_screener_snapshot_tasks in TASKIQ_TASK_MODULES
    task = invest_screener_snapshot_tasks.build_invest_screener_snapshots
    labels = getattr(task, "labels", {}) or {}
    assert labels.get("schedule") is None


@pytest.mark.asyncio
async def test_snapshot_task_returns_serializable_summary(monkeypatch):
    from app.jobs.invest_screener_snapshots import SnapshotBuildResult, SnapshotSample
    from app.tasks import invest_screener_snapshot_tasks

    async def fake_run_snapshot_build(request):
        assert request.market == "kr"
        assert request.commit is False
        assert request.all_symbols is True
        return SnapshotBuildResult(
            market="kr",
            symbols_resolved=1,
            snapshots_built=1,
            skipped=0,
            committed=False,
            batches=1,
            started_at=dt.datetime(2026, 5, 12, 1, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 12, 1, 1, tzinfo=dt.UTC),
            snapshot_date_distribution={"2026-05-12": 1},
            samples=(
                SnapshotSample(
                    market="kr",
                    symbol="005930",
                    snapshot_date=dt.date(2026, 5, 12),
                    latest_close="78500",
                    consecutive_up_days=3,
                    week_change_rate="4.66",
                ),
            ),
            warnings=("dry-run sample",),
        )

    monkeypatch.setattr(
        invest_screener_snapshot_tasks,
        "run_snapshot_build",
        fake_run_snapshot_build,
    )

    task = invest_screener_snapshot_tasks.build_invest_screener_snapshots
    raw_func = getattr(task, "original_func", task)
    result = await raw_func(market="kr", all_symbols=True)

    assert result == {
        "market": "kr",
        "symbolsResolved": 1,
        "snapshotsBuilt": 1,
        "skipped": 0,
        "committed": False,
        "batches": 1,
        "startedAt": "2026-05-12T01:00:00+00:00",
        "finishedAt": "2026-05-12T01:01:00+00:00",
        "snapshotDateDistribution": {"2026-05-12": 1},
        "samples": [
            {
                "market": "kr",
                "symbol": "005930",
                "snapshotDate": "2026-05-12",
                "latestClose": "78500",
                "consecutiveUpDays": 3,
                "weekChangeRate": "4.66",
            }
        ],
        "warnings": ["dry-run sample"],
    }
