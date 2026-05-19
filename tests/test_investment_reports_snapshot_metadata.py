"""ROB-269 Phase 3 — Snapshot metadata round-trip + DB CHECK guard.

Verifies the additive Phase 3 schema changes on ``review.investment_reports``:

1. IngestReportRequest accepts the 6 new optional fields and they round-trip
   through the ingestion service to the persisted row.
2. Legacy reports (no snapshot fields) still ingest and read back cleanly.
3. The Decision 4 layer (i) DB CHECK rejects ``published`` rows whose
   ``snapshot_freshness_summary['overall']`` is a stale status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport
from app.schemas.investment_reports import IngestReportRequest
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)

# ``session`` fixture is auto-discovered via the pytest_plugins entry in
# tests/conftest.py registering ``tests._investment_reports_helpers``.


def _base_request(**overrides) -> IngestReportRequest:
    payload: dict = {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "snapshot metadata smoke",
        "summary": "phase 3 round-trip",
        "kst_date": f"2026-05-{19 + len(overrides):02d}",
        "generator_version": "v1",
    }
    payload.update(overrides)
    return IngestReportRequest(**payload)


@pytest.mark.asyncio
async def test_ingest_persists_snapshot_metadata_fields(session: AsyncSession) -> None:
    """All 6 fields round-trip through ingestion to the DB row."""
    bundle_uuid = uuid.uuid4()
    request = _base_request(
        snapshot_bundle_uuid=bundle_uuid,
        snapshot_policy_version="intraday_action_report_v1",
        snapshot_coverage_summary={
            "required": {"portfolio": "fresh", "market": "fresh"},
            "optional": {"news": "unavailable"},
        },
        snapshot_freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
        source_conflicts={"naver_vs_kis": "minor_price_drift"},
        unavailable_sources={"naver": "확인 불가", "toss": "soft_stale"},
    )

    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()

    row = await session.scalar(
        sa.select(InvestmentReport).where(InvestmentReport.id == report.id)
    )
    assert row is not None
    assert row.snapshot_bundle_uuid == bundle_uuid
    assert row.snapshot_policy_version == "intraday_action_report_v1"
    assert row.snapshot_coverage_summary["required"]["portfolio"] == "fresh"
    assert row.snapshot_freshness_summary["overall"] == "partial"
    assert row.source_conflicts == {"naver_vs_kis": "minor_price_drift"}
    assert row.unavailable_sources["toss"] == "soft_stale"


@pytest.mark.asyncio
async def test_legacy_report_without_snapshot_fields_still_ingests(
    session: AsyncSession,
) -> None:
    """Backward-compat path — pre-Phase-3 callers omit all 6 fields."""
    request = _base_request()
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()

    row = await session.scalar(
        sa.select(InvestmentReport).where(InvestmentReport.id == report.id)
    )
    assert row is not None
    assert row.snapshot_bundle_uuid is None
    assert row.snapshot_policy_version is None
    assert row.snapshot_coverage_summary is None
    assert row.snapshot_freshness_summary is None
    assert row.source_conflicts is None
    assert row.unavailable_sources is None


@pytest.mark.asyncio
async def test_db_check_rejects_published_with_hard_stale_freshness(
    session: AsyncSession,
) -> None:
    """Decision 4 layer (i): published + freshness.overall='hard_stale' → IntegrityError."""
    # Insert a row directly via ORM so we can craft an explicit violation.
    row = InvestmentReport(
        idempotency_key=f"phase3-check-test-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="should fail",
        summary="hard_stale overall",
        market_snapshot={},
        portfolio_snapshot={},
        status="published",
        snapshot_freshness_summary={"overall": "hard_stale"},
        published_at=datetime.now(UTC),
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_db_check_rejects_published_with_failed_freshness(
    session: AsyncSession,
) -> None:
    """Decision 4 layer (i): published + freshness.overall='failed' → IntegrityError."""
    row = InvestmentReport(
        idempotency_key=f"phase3-check-test-failed-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="should fail",
        summary="failed overall",
        market_snapshot={},
        portfolio_snapshot={},
        status="published",
        snapshot_freshness_summary={"overall": "failed"},
        published_at=datetime.now(UTC),
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_db_check_allows_published_with_partial_freshness(
    session: AsyncSession,
) -> None:
    """Decision 4 layer (i): partial overall is acceptable (not hard-stale)."""
    request = _base_request(
        status="published",
        published_at=datetime.now(UTC),
        snapshot_freshness_summary={"overall": "partial"},
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.status == "published"


@pytest.mark.asyncio
async def test_db_check_allows_draft_with_hard_stale_freshness(
    session: AsyncSession,
) -> None:
    """Draft status bypasses the CHECK — only the published transition is gated."""
    request = _base_request(
        status="draft",
        snapshot_freshness_summary={"overall": "hard_stale"},
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.status == "draft"


# ---------------------------------------------------------------------------
# NULL-semantics regression tests for the corrected CHECK (20260519_rob269_p3a)
# ---------------------------------------------------------------------------
#
# Before the p3a follow-up migration the CHECK predicate evaluated to UNKNOWN
# (not FALSE) when ``snapshot_freshness_summary`` was set but ``overall`` was
# missing / JSON-null, and PostgreSQL CHECK accepts UNKNOWN. The corrected
# predicate uses an explicit ``IS NOT NULL`` guard so those cases now reject.
@pytest.mark.asyncio
async def test_db_check_rejects_published_with_missing_overall_key(
    session: AsyncSession,
) -> None:
    """``snapshot_freshness_summary`` exists but has no ``overall`` key → reject."""
    row = InvestmentReport(
        idempotency_key=f"phase3-check-missing-overall-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="should fail",
        summary="missing overall key",
        market_snapshot={},
        portfolio_snapshot={},
        status="published",
        # ``overall`` deliberately absent — must be rejected.
        snapshot_freshness_summary={"portfolio": {"status": "fresh"}},
        published_at=datetime.now(UTC),
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_db_check_rejects_published_with_empty_freshness_summary_object(
    session: AsyncSession,
) -> None:
    """Empty ``snapshot_freshness_summary = {}`` is also missing ``overall``."""
    row = InvestmentReport(
        idempotency_key=f"phase3-check-empty-summary-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="should fail",
        summary="empty freshness object",
        market_snapshot={},
        portfolio_snapshot={},
        status="published",
        snapshot_freshness_summary={},
        published_at=datetime.now(UTC),
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_db_check_rejects_published_with_overall_explicit_null(
    session: AsyncSession,
) -> None:
    """``overall: null`` (JSON null) must be treated as missing — reject."""
    row = InvestmentReport(
        idempotency_key=f"phase3-check-null-overall-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="should fail",
        summary="overall=null",
        market_snapshot={},
        portfolio_snapshot={},
        status="published",
        snapshot_freshness_summary={"overall": None},
        published_at=datetime.now(UTC),
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_db_check_rejects_published_with_unavailable_overall(
    session: AsyncSession,
) -> None:
    """Belt-and-suspenders: ``unavailable`` is also out of the allow-set."""
    row = InvestmentReport(
        idempotency_key=f"phase3-check-unavailable-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="should fail",
        summary="overall=unavailable",
        market_snapshot={},
        portfolio_snapshot={},
        status="published",
        snapshot_freshness_summary={"overall": "unavailable"},
        published_at=datetime.now(UTC),
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_db_check_allows_published_with_overall_fresh(
    session: AsyncSession,
) -> None:
    """Positive case — ``fresh`` overall is in the allow-set."""
    request = _base_request(
        status="published",
        published_at=datetime.now(UTC),
        snapshot_freshness_summary={"overall": "fresh"},
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.status == "published"


@pytest.mark.asyncio
async def test_db_check_allows_published_with_overall_soft_stale(
    session: AsyncSession,
) -> None:
    """Positive case — ``soft_stale`` overall is in the allow-set."""
    request = _base_request(
        status="published",
        published_at=datetime.now(UTC),
        snapshot_freshness_summary={"overall": "soft_stale"},
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.commit()
    assert report.status == "published"
