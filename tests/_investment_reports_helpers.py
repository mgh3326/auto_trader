"""Shared test scaffolding for ROB-265 investment_reports tests.

Centralises the per-test async session fixture, the cleanup-table
list, the shared-DB guard lock, and a couple of small helpers so the ORM, schema, repository,
ingestion, decisions, watch-activation, and query-service test files
don't each re-declare the same boilerplate (Sonar duplicated-line fix).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentReportNewsCitation,
    InvestmentReportNewsFetchRun,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
from tests._run_owned_database import uses_shared_test_database

INVESTMENT_REPORTS_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
    InvestmentReportItemDecision.__table__,
    InvestmentReportNewsCitation.__table__,
    InvestmentReportNewsFetchRun.__table__,
    InvestmentWatchAlert.__table__,
    InvestmentWatchEvent.__table__,
]
INVESTMENT_REPORTS_TEST_LOCK_ID = 265_202_605


@pytest_asyncio.fixture
async def session(_bootstrap_test_schema) -> AsyncSession:
    """Rollback-isolated AsyncSession against the current PostgreSQL test DB.

    Schema is owned by the session-scoped ``_bootstrap_test_schema`` barrier
    (ROB-723) — this fixture performs no DDL. A SAVEPOINT-aware session lets
    tests exercise ``commit()`` while the outer transaction remains owned by
    the fixture and is rolled back after the test. The shared-DB opt-in keeps
    the legacy advisory lock; ordinary run-owned DBs need no serialization.
    """
    import sqlalchemy as sa

    from app.core.db import engine

    guard = None
    try:
        if uses_shared_test_database():
            guard = await engine.connect()
            await guard.execute(
                sa.text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
        async with engine.connect() as connection:
            outer = await connection.begin()
            try:
                factory = async_sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                async with factory() as sess:
                    yield sess
            finally:
                if outer.is_active:
                    await outer.rollback()
    finally:
        if guard is not None:
            try:
                await guard.execute(
                    sa.text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )
            finally:
                await guard.close()


@pytest_asyncio.fixture
async def committed_investment_reports_session(
    _bootstrap_test_schema,
) -> AsyncSession:
    """Session for tests whose production handler opens independent sessions.

    Those commits cannot participate in the fixture's outer transaction. Keep
    this exceptional path explicit and clean only its seven-table family with
    row-level DELETEs, avoiding repeated engines and AccessExclusive TRUNCATE.
    """
    import sqlalchemy as sa

    from app.core.db import AsyncSessionLocal, engine

    async def _delete_rows() -> None:
        async with engine.begin() as connection:
            for table in reversed(INVESTMENT_REPORTS_TABLES):
                await connection.execute(sa.text(f'DELETE FROM review."{table.name}"'))

    guard = None
    try:
        if uses_shared_test_database():
            guard = await engine.connect()
            await guard.execute(
                sa.text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
        await _delete_rows()
        try:
            async with AsyncSessionLocal() as sess:
                yield sess
        finally:
            await _delete_rows()
    finally:
        if guard is not None:
            try:
                await guard.execute(
                    sa.text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )
            finally:
                await guard.close()


def future_datetime(days: int = 7) -> datetime:
    """Return a TZ-aware datetime ``days`` in the future (default 7)."""
    return datetime.now(UTC) + timedelta(days=days)


async def publish_report(session: AsyncSession, report: InvestmentReport) -> None:
    """ROB-352: flip a report to ``status='published'`` for prior-context tests.

    Clears ``snapshot_freshness_summary`` to SQL NULL so the DB CHECK constraint
    ``ck_investment_reports_no_published_on_hard_stale`` is satisfied. Direct SQL
    avoids asyncpg serialising Python ``None`` → JSON ``null`` (which the
    constraint would reject). Reports default to ``draft`` on ingest, and Slice B
    excludes drafts from ``previous_report_context`` — so tests that expect a
    report to appear as prior context must publish it first.
    """
    import sqlalchemy as sa

    await session.execute(
        sa.text(
            "UPDATE review.investment_reports"
            " SET status = 'published', snapshot_freshness_summary = NULL"
            " WHERE id = :id"
        ).bindparams(id=report.id)
    )
    await session.flush()
    await session.refresh(report)


async def assert_integrity_error(session: AsyncSession, *rows: object) -> None:
    """Add ``rows``, commit, expect ``IntegrityError``, then rollback.

    Common shape for DB-constraint tests (CHECK / UNIQUE / FK).
    """
    import sqlalchemy as sa

    for row in rows:
        session.add(row)
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()
