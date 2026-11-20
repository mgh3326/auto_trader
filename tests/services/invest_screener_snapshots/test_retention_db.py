"""DB retention step for invest_screener_snapshots (hk 5752).

Runs the real ``prune_snapshot_history`` job against the test database, but
routes its sessions through one shared transaction so commits are only flushes
and the whole test is rolled back at the end. Other tests share this database.
Dates are placed in 2090 so they are the newest rows in the market.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.jobs import invest_screener_snapshots as job
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)

SYMBOL = "RETDBTST"
BASE = dt.date(2090, 1, 1)


class _NoCommitProxy:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self):
        await self._session.flush()


class _SharedSession:
    def __init__(self, session):
        self._proxy = _NoCommitProxy(session)

    async def __aenter__(self):
        return self._proxy

    async def __aexit__(self, *exc):
        return False


def _payload(day: dt.date) -> SnapshotUpsert:
    return SnapshotUpsert(
        market="us",
        symbol=SYMBOL,
        snapshot_date=day,
        latest_close=Decimal("10"),
        source="yahoo",
    )


async def _seed(session, days: int) -> list[dt.date]:
    repo = InvestScreenerSnapshotsRepository(session)
    dates = [BASE + dt.timedelta(days=i) for i in range(days)]
    for day in dates:
        await repo.upsert(_payload(day))
    await session.flush()
    return dates


async def _our_dates(session) -> list[dt.date]:
    result = await session.execute(
        sa.select(InvestScreenerSnapshot.snapshot_date)
        .where(
            InvestScreenerSnapshot.market == "us",
            InvestScreenerSnapshot.symbol == SYMBOL,
        )
        .order_by(InvestScreenerSnapshot.snapshot_date)
    )
    return [row[0] for row in result.all()]


@pytest_asyncio.fixture
async def shared_session(db_session, monkeypatch):
    monkeypatch.setattr(job, "AsyncSessionLocal", lambda: _SharedSession(db_session))
    yield db_session
    await db_session.rollback()


@pytest.mark.asyncio
async def test_commit_keeps_exactly_the_latest_five_dates(shared_session) -> None:
    dates = await _seed(shared_session, days=7)

    result = await job.prune_snapshot_history(
        "us", keep_trading_days=5, commit=True, batch_size=2
    )

    assert result.committed is True
    assert result.cutoff == dates[2]
    assert result.rows >= 2  # at least our two oldest rows; other rows may exist
    assert (await _our_dates(shared_session)) == dates[2:]


@pytest.mark.asyncio
async def test_rerun_after_completion_deletes_nothing(shared_session) -> None:
    await _seed(shared_session, days=7)
    await job.prune_snapshot_history("us", keep_trading_days=5, commit=True)

    again = await job.prune_snapshot_history("us", keep_trading_days=5, commit=True)

    assert again.rows == 0


@pytest.mark.asyncio
async def test_dry_run_counts_without_deleting(shared_session) -> None:
    dates = await _seed(shared_session, days=7)

    result = await job.prune_snapshot_history("us", keep_trading_days=5, commit=False)

    assert result.committed is False
    assert result.cutoff == dates[2]
    assert result.rows >= 2
    assert (await _our_dates(shared_session)) == dates


@pytest.mark.asyncio
async def test_huge_keep_is_a_no_op(shared_session) -> None:
    dates = await _seed(shared_session, days=7)

    result = await job.prune_snapshot_history(
        "us", keep_trading_days=100_000, commit=True
    )

    assert result.cutoff is None
    assert result.rows == 0
    assert (await _our_dates(shared_session)) == dates


def _support_payload(day: dt.date) -> SnapshotUpsert:
    return SnapshotUpsert(
        market="us",
        symbol=SYMBOL,
        snapshot_date=day,
        latest_close=Decimal("10"),
        source="yahoo",
        support_price=Decimal("9"),
        support_kind="bb_lower",
        support_strength="strong",
        support_computed_at=dt.datetime(2090, 1, 1, tzinfo=dt.UTC),
    )


@pytest.mark.asyncio
async def test_support_partition_is_never_deleted(shared_session) -> None:
    """The support-proximity preset reads the newest support-computed partition, so
    retention keeps it even when it is older than the five newest ordinary dates
    (hk 5752 r1)."""
    dates = [BASE + dt.timedelta(days=i) for i in range(7)]
    repo = InvestScreenerSnapshotsRepository(shared_session)
    await repo.upsert(_support_payload(dates[0]))  # the only support-computed row
    for day in dates[1:]:
        await repo.upsert(_payload(day))
    await shared_session.flush()

    result = await job.prune_snapshot_history(
        "us", keep_trading_days=5, commit=True, batch_size=2
    )

    assert result.preserved_support_date == dates[0]
    assert (await _our_dates(shared_session)) == [dates[0], *dates[2:]]


@pytest.mark.asyncio
async def test_dry_run_with_pending_dates_matches_the_commit(shared_session) -> None:
    """A dry-run whose build writes a newer date projects the same cutoff the commit
    computes after writing (hk 5752 r1)."""
    dates = await _seed(shared_session, days=5)  # five stored dates, all kept
    pending_day = BASE + dt.timedelta(days=5)

    preview = await job.prune_snapshot_history(
        "us",
        keep_trading_days=5,
        commit=False,
        pending_dates={pending_day: 1},
    )

    assert preview.cutoff == dates[1]  # union newest-five: pending + dates[1:]
    assert preview.pending_rows_upper_bound == 0  # pending day is newer than cutoff
    assert (await _our_dates(shared_session)) == dates  # dry-run deleted nothing

    await _seed_one(shared_session, pending_day)
    committed = await job.prune_snapshot_history("us", keep_trading_days=5, commit=True)

    assert committed.cutoff == preview.cutoff
    assert (await _our_dates(shared_session)) == dates[1:] + [pending_day]


async def _seed_one(session, day: dt.date) -> None:
    await InvestScreenerSnapshotsRepository(session).upsert(_payload(day))
    await session.flush()
