"""Shared test scaffolding for ROB-265 investment_reports tests.

Centralises the per-test async session fixture, the truncated-table
list, the xdist guard lock, and a couple of small helpers so the ORM, schema, repository,
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
    create_async_engine,
)

from app.core.config import settings
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentReportNewsCitation,
    InvestmentReportNewsFetchRun,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)

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
async def session() -> AsyncSession:
    """Per-test AsyncSession against the real PostgreSQL test_db.

    Schema is owned by the session-scoped ``_bootstrap_test_schema`` barrier
    (ROB-723) — this fixture performs no DDL. Between tests it TRUNCATEs the
    investment-report table family, serialized against the conftest cleanup by
    the shared advisory lock and made deadlock-resilient by run_with_deadlock_retry.
    """
    import sqlalchemy as sa

    from tests._db_retry import run_with_deadlock_retry

    engine = create_async_engine(settings.DATABASE_URL, future=True)

    async def _truncate() -> None:
        async with engine.begin() as conn:
            for table in reversed(INVESTMENT_REPORTS_TABLES):
                await conn.execute(
                    sa.text(
                        f'TRUNCATE TABLE review."{table.name}" '
                        "RESTART IDENTITY CASCADE"
                    )
                )

    try:
        async with engine.connect() as guard:
            await guard.execute(
                sa.text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
            try:
                await run_with_deadlock_retry(_truncate)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as sess:
                    try:
                        yield sess
                    finally:
                        await sess.rollback()
                await run_with_deadlock_retry(_truncate)
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
