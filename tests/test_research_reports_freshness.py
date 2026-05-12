"""ROB-207 freshness/readiness tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_no_runs_is_unavailable(db_session):
    from app.services.research_reports.freshness import (
        compute_research_reports_readiness,
    )
    source = f"empty_source_{uuid4()}"
    out = await compute_research_reports_readiness(
        db_session, source=source, max_age_hours=24
    )
    assert out.is_ready is False
    assert out.is_stale is True
    assert "research_reports_unavailable" in out.warnings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_recent_finished_run_is_ready(db_session):
    from app.models.research_reports import ResearchReportIngestionRun
    from app.services.research_reports.freshness import (
        compute_research_reports_readiness,
    )
    source = f"src_{uuid4()}"
    now = datetime.now(UTC)
    db_session.add(
        ResearchReportIngestionRun(
            run_uuid=f"run-{uuid4()}",
            payload_version="research-reports.v1",
            source=source,
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
            report_count=3,
            inserted_count=3,
            skipped_count=0,
        )
    )
    await db_session.commit()
    out = await compute_research_reports_readiness(
        db_session, source=source, max_age_hours=24
    )
    assert out.is_ready is True
    assert out.is_stale is False
    assert out.latest_inserted_count == 3
    assert out.latest_finished_at is not None
    assert out.warnings == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_unfinished_run_emits_warning(db_session):
    from app.models.research_reports import ResearchReportIngestionRun
    from app.services.research_reports.freshness import (
        compute_research_reports_readiness,
    )
    source = f"src_{uuid4()}"
    db_session.add(
        ResearchReportIngestionRun(
            run_uuid=f"run-{uuid4()}",
            payload_version="research-reports.v1",
            source=source,
            started_at=datetime.now(UTC),
            finished_at=None,
            report_count=10,
        )
    )
    await db_session.commit()
    out = await compute_research_reports_readiness(
        db_session, source=source, max_age_hours=24
    )
    assert out.is_ready is False
    assert "research_reports_run_unfinished" in out.warnings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_stale_when_finished_older_than_budget(db_session):
    from app.models.research_reports import ResearchReportIngestionRun
    from app.services.research_reports.freshness import (
        compute_research_reports_readiness,
    )
    source = f"src_{uuid4()}"
    old = datetime.now(UTC) - timedelta(hours=48)
    db_session.add(
        ResearchReportIngestionRun(
            run_uuid=f"run-{uuid4()}",
            payload_version="research-reports.v1",
            source=source,
            started_at=old - timedelta(minutes=5),
            finished_at=old,
            report_count=2,
            inserted_count=2,
            skipped_count=0,
        )
    )
    await db_session.commit()
    out = await compute_research_reports_readiness(
        db_session, source=source, max_age_hours=24
    )
    assert out.is_ready is False
    assert out.is_stale is True
    assert "research_reports_stale" in out.warnings


@pytest.mark.unit
def test_response_schema_has_no_body_fields():
    """ROB-140 guardrail — readiness response must not leak body-style fields."""
    from app.schemas.research_reports import ResearchReportsReadinessResponse

    schema = ResearchReportsReadinessResponse.model_json_schema()
    forbidden = {
        "pdf_body", "pdf_text", "full_text",
        "article_content", "article_body", "raw_payload",
        "summary_text", "detail_excerpt",
    }
    assert forbidden.isdisjoint(schema.get("properties", {}).keys())
