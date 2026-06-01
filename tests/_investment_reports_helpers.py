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
                    # Start clean as well as cleaning up after the test. Global
                    # db_session-based cross-domain tests can commit rows into
                    # these tables, and interrupted local/CI runs can leave
                    # stale rows behind before this fixture's first test starts.
                    for table in reversed(INVESTMENT_REPORTS_TABLES):
                        table_name = table.name  # type: ignore[attr-defined]
                        await conn.execute(
                            sa.text(
                                f'TRUNCATE TABLE review."{table_name}" '
                                "RESTART IDENTITY CASCADE"
                            )
                        )
                    # ROB-269 Phase 3 — additive snapshot metadata + CHECK.
                    # create_all with checkfirst=True skips existing tables,
                    # so persistent test DBs miss the new columns. These are
                    # idempotent and mirror the alembic migration exactly.
                    for stmt in (
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_bundle_uuid UUID",
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_policy_version TEXT",
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_coverage_summary JSONB",
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_freshness_summary JSONB",
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS source_conflicts JSONB",
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS unavailable_sources JSONB",
                        # ROB-318 Phase 3 (PR-B) — deterministic report diagnostics.
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_report_diagnostics JSONB",
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_investment_reports_snapshot_bundle_uuid "
                        "ON review.investment_reports (snapshot_bundle_uuid)",
                        # ROB-269 Phase 3 (corrected by 20260519_rob269_p3a):
                        # explicit ``IS NOT NULL`` guard prevents CHECK from
                        # accepting UNKNOWN when ``overall`` is missing or
                        # JSON-null.
                        "ALTER TABLE review.investment_reports "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_reports_no_published_on_hard_stale",
                        "ALTER TABLE review.investment_reports "
                        "ADD CONSTRAINT "
                        "ck_investment_reports_no_published_on_hard_stale "
                        "CHECK ("
                        "status <> 'published' "
                        "OR snapshot_freshness_summary IS NULL "
                        "OR ("
                        "(snapshot_freshness_summary->>'overall') IS NOT NULL "
                        "AND (snapshot_freshness_summary->>'overall') IN "
                        "('fresh','soft_stale','partial')"
                        "))",
                        # ROB-274 — proposal-state columns + operation-aware
                        # CHECKs on investment_report_items. Idempotent and
                        # mirrors migration 20260520_rob274_p1 exactly.
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS operation TEXT",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS target_ref JSONB",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS current_state JSONB",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS proposed_state JSONB",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS diff JSONB",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS apply_policy TEXT",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS decision_bucket TEXT",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS cited_symbol_report_uuid UUID",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS cited_dimension_report_uuids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
                        "ALTER TABLE review.investment_report_items "
                        "ADD COLUMN IF NOT EXISTS cited_snapshot_uuids "
                        "UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS ck_investment_report_items_ck_investment_report_items_decision_bucket",
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS ck_investment_report_items_decision_bucket",
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT ck_investment_report_items_decision_bucket "
                        "CHECK ("
                        "decision_bucket IS NULL OR decision_bucket IN ("
                        "'new_buy_candidate','open_action','completed_or_existing','deferred_no_action','risk_watch'"
                        "))",
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_investment_report_items_operation_kind "
                        "ON review.investment_report_items "
                        "(operation, item_kind, status)",
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_operation",
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT ck_investment_report_items_operation "
                        "CHECK ("
                        "operation IS NULL OR operation IN ("
                        "'create','modify','cancel','keep','replace','review'"
                        "))",
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_apply_policy",
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT ck_investment_report_items_apply_policy "
                        "CHECK ("
                        "apply_policy IS NULL "
                        "OR apply_policy = 'requires_user_approval'"
                        ")",
                        # Watch invariants — drop both canonical + hashed
                        # names (see 20260520_rob274_p1 docstring) before
                        # recreating with the operation-aware predicate.
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        '"ck_investment_report_items_ck_investment_report_items_w_421e"',
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_watch_has_condition",
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT "
                        "ck_investment_report_items_watch_has_condition "
                        "CHECK ("
                        "item_kind <> 'watch' "
                        "OR operation IN ('cancel','keep','review') "
                        "OR watch_condition IS NOT NULL"
                        ")",
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        '"ck_investment_report_items_ck_investment_report_items_w_fdaa"',
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_watch_has_expiry",
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT "
                        "ck_investment_report_items_watch_has_expiry "
                        "CHECK ("
                        "item_kind <> 'watch' "
                        "OR operation IN ('cancel','keep','review') "
                        "OR valid_until IS NOT NULL"
                        ")",
                        # ROB-403 — investment_watch_alerts conditions/combine/
                        # threshold_high + operator CHECK extend. Idempotent;
                        # mirrors the alembic migration.
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS conditions JSONB "
                        "NOT NULL DEFAULT '[]'::jsonb",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS combine TEXT NOT NULL DEFAULT 'and'",
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_operator",
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_ck_investment_watch_alerts_operator",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_operator "
                        "CHECK (operator IN ('above','below','between'))",
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_combine",
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_ck_investment_watch_alerts_combine",
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_combine "
                        "CHECK (combine IN ('and'))",
                        # ROB-403 — investment_watch_events: between + threshold_high.
                        "ALTER TABLE review.investment_watch_events "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)",
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_events_operator",
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS ck_investment_watch_events_ck_investment_watch_events_operator",
                        "ALTER TABLE review.investment_watch_events "
                        "ADD CONSTRAINT ck_investment_watch_events_operator "
                        "CHECK (operator IN ('above','below','between'))",
                    ):
                        await conn.execute(sa.text(stmt))
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


async def publish_report(session: AsyncSession, report: InvestmentReport) -> None:
    """ROB-352: flip a report to ``status='published'`` for prior-context tests.

    Clears ``snapshot_freshness_summary`` to SQL NULL so the DB CHECK constraint
    ``ck_investment_reports_no_published_on_hard_stale`` is satisfied. Direct SQL
    avoids asyncpg serialising Python ``None`` → JSON ``null`` (which the
    constraint would reject). Reports default to ``draft`` on ingest, and Slice B
    excludes drafts from ``previous_report_context`` — so tests that expect a
    report to appear as prior context must publish it first.
    """
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
    for row in rows:
        session.add(row)
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()
