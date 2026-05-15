from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.jobs import invest_crypto_screener_snapshots as snapshot_job
from app.services.invest_crypto_screener_snapshots import coverage_service
from app.services.invest_crypto_screener_snapshots.freshness import (
    classify_crypto_partition,
    today_crypto_snapshot_date,
)
from app.services.invest_crypto_screener_snapshots.repository import (
    CryptoCoverageCounts,
)


def test_today_crypto_snapshot_date_does_not_weekend_roll_back() -> None:
    saturday_kst = dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC)

    assert today_crypto_snapshot_date(saturday_kst) == dt.date(2026, 5, 16)


def test_classify_crypto_partition_states() -> None:
    now = dt.datetime(2026, 5, 13, 5, 0, tzinfo=dt.UTC)
    today = dt.date(2026, 5, 13)

    assert (
        classify_crypto_partition(
            latest_partition_date=None,
            row_count=0,
            last_computed_at=None,
            today=today,
            now=now,
        )
        == "missing"
    )
    assert (
        classify_crypto_partition(
            latest_partition_date=today,
            row_count=50,
            last_computed_at=now - dt.timedelta(hours=1),
            today=today,
            now=now,
        )
        == "fresh"
    )
    assert (
        classify_crypto_partition(
            latest_partition_date=today,
            row_count=5,
            last_computed_at=now - dt.timedelta(hours=1),
            today=today,
            now=now,
        )
        == "partial"
    )
    assert (
        classify_crypto_partition(
            latest_partition_date=today - dt.timedelta(days=1),
            row_count=50,
            last_computed_at=now - dt.timedelta(hours=1),
            today=today,
            now=now,
        )
        == "stale"
    )
    assert (
        classify_crypto_partition(
            latest_partition_date=today,
            row_count=50,
            last_computed_at=now - dt.timedelta(hours=4),
            today=today,
            now=now,
        )
        == "stale"
    )


@pytest.mark.asyncio
async def test_build_crypto_coverage_reports_repository_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRepository:
        def __init__(self, session: object) -> None:
            assert session == "session"

        async def coverage(self, *, today: dt.date) -> Any:
            return CryptoCoverageCounts(
                latest_partition_date=today,
                latest_partition_count=12,
                stale_count=4,
                last_computed_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30),
            )

    monkeypatch.setattr(
        coverage_service,
        "InvestCryptoScreenerSnapshotsRepository",
        FakeRepository,
    )

    report = await coverage_service.build_crypto_coverage("session")  # type: ignore[arg-type]

    assert report.market == "crypto"
    assert report.latestPartitionDate == today_crypto_snapshot_date(report.asOf)
    assert report.snapshotsInLatestPartition == 12
    assert report.snapshotsStale == 4
    assert report.dataState == "partial"


@pytest.mark.asyncio
async def test_run_crypto_snapshot_build_rolls_back_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            events.append(("enter", self))
            return self

        async def __aexit__(self, *_exc: object) -> None:
            events.append(("exit", self))

        async def commit(self) -> None:
            events.append(("commit", self))

        async def rollback(self) -> None:
            events.append(("rollback", self))

    fake_session = FakeSession()

    def fake_session_factory() -> FakeSession:
        return fake_session

    async def fake_build_crypto_snapshots(**kwargs: object) -> dict[str, object]:
        events.append(("limit", kwargs["limit"]))
        events.append(("commit_flag", kwargs["commit"]))
        return {"inserted": 3}

    monkeypatch.setattr(snapshot_job, "AsyncSessionLocal", fake_session_factory)
    monkeypatch.setattr(
        snapshot_job, "build_crypto_snapshots", fake_build_crypto_snapshots
    )

    result = await snapshot_job.run_crypto_snapshot_build(
        snapshot_job.CryptoSnapshotBuildRequest(limit=7, commit=False)
    )

    assert result == {"inserted": 3}
    assert ("limit", 7) in events
    assert ("commit_flag", False) in events
    assert ("rollback", fake_session) in events
    assert ("commit", fake_session) not in events


@pytest.mark.asyncio
async def test_run_crypto_snapshot_build_commits_all_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def commit(self) -> None:
            events.append(("commit", self))

        async def rollback(self) -> None:
            events.append(("rollback", self))

    fake_session = FakeSession()

    async def fake_build_crypto_snapshots(**kwargs: object) -> dict[str, object]:
        events.append(("limit", kwargs["limit"]))
        events.append(("commit_flag", kwargs["commit"]))
        return {"inserted": 50}

    monkeypatch.setattr(snapshot_job, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        snapshot_job, "build_crypto_snapshots", fake_build_crypto_snapshots
    )

    result = await snapshot_job.run_crypto_snapshot_build(
        snapshot_job.CryptoSnapshotBuildRequest(all_markets=True, commit=True)
    )

    assert result == {"inserted": 50}
    assert ("limit", None) in events
    assert ("commit_flag", True) in events
    assert ("commit", fake_session) in events
    assert ("rollback", fake_session) not in events
