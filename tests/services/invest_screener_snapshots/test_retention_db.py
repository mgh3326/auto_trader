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
