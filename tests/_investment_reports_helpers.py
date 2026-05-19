"""Shared test scaffolding for ROB-265 investment_reports tests.

Centralises the per-test async session fixture, the truncated-table
list, the xdist guard lock, and a couple of small helpers so the ORM, schema, repository,
ingestion, decisions, watch-activation, and query-service test files
don't each re-declare the same boilerplate (Sonar duplicated-line fix).

The fixture creates the 5 tables idempotently (checkfirst=True so the
alembic migration can co-own them) and truncates between tests so
the schema stays intact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.base import Base
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)

INVESTMENT_REPORTS_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
    InvestmentReportItemDecision.__table__,
    InvestmentWatchAlert.__table__,
    InvestmentWatchEvent.__table__,
]
INVESTMENT_REPORTS_TEST_LOCK_ID = 265_202_605


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Per-test AsyncSession against the real PostgreSQL test_db.

    Tables are created idempotently (no-op if the alembic migration
    already owns them). Between tests rows are truncated CASCADE so
    other tests start clean without dropping the schema.

    CI runs pytest-xdist with ``--dist=loadfile``. These tests share the
    same PostgreSQL schema, and concurrent per-test ``TRUNCATE ... CASCADE``
    can deadlock with another worker's inserts. A session-level advisory lock
    serializes only this investment-report fixture while leaving the rest of
    the suite parallel.
    """
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    try:
        async with engine.connect() as guard:
            await guard.execute(
                sa.text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(
                        Base.metadata.create_all,
                        tables=INVESTMENT_REPORTS_TABLES,
                        checkfirst=True,
                    )
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as sess:
                    try:
                        yield sess
                    finally:
                        await sess.rollback()
                async with factory() as cleanup:
                    for table in reversed(INVESTMENT_REPORTS_TABLES):
                        await cleanup.execute(
                            sa.text(
                                f'TRUNCATE TABLE review."{table.name}" RESTART IDENTITY CASCADE'
                            )
                        )
                    await cleanup.commit()
            finally:
                await guard.execute(
                    sa.text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )
    finally:
        await engine.dispose()


def future_datetime(days: int = 7) -> datetime:
    """Return a TZ-aware datetime ``days`` in the future (default 7)."""
    return datetime.now(UTC) + timedelta(days=days)


async def assert_integrity_error(session: AsyncSession, *rows: object) -> None:
    """Add ``rows``, commit, expect ``IntegrityError``, then rollback.

    Common shape for DB-constraint tests (CHECK / UNIQUE / FK).
    """
    for row in rows:
        session.add(row)
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()
