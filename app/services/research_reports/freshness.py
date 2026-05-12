"""ROB-207 freshness/readiness for research_reports.

Read-only. Derives the freshness signal from research_report_ingestion_runs.
Never returns body / excerpt / summary fields.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_reports import ResearchReportIngestionRun
from app.schemas.research_reports import ResearchReportsReadinessResponse


async def compute_research_reports_readiness(
    db: AsyncSession,
    *,
    source: str | None = None,
    max_age_hours: int = 24,
) -> ResearchReportsReadinessResponse:
    stmt = select(ResearchReportIngestionRun).order_by(
        ResearchReportIngestionRun.received_at.desc(),
        ResearchReportIngestionRun.id.desc(),
    )
    if source is not None:
        stmt = stmt.where(ResearchReportIngestionRun.source == source)
    stmt = stmt.limit(1)
    latest = (await db.execute(stmt)).scalar_one_or_none()

    warnings: list[str] = []
    if latest is None:
        warnings.append("research_reports_unavailable")
        return ResearchReportsReadinessResponse(
            source=source,
            is_ready=False,
            is_stale=True,
            warnings=warnings,
            max_age_hours=max_age_hours,
        )

    if latest.finished_at is None:
        warnings.append("research_reports_run_unfinished")

    is_stale = True
    if latest.finished_at is not None:
        threshold = datetime.now(UTC) - timedelta(hours=max_age_hours)
        is_stale = latest.finished_at < threshold

    if is_stale and "research_reports_run_unfinished" not in warnings:
        warnings.append("research_reports_stale")

    is_ready = latest.finished_at is not None and not is_stale

    return ResearchReportsReadinessResponse(
        source=source,
        is_ready=is_ready,
        is_stale=is_stale,
        latest_run_uuid=latest.run_uuid,
        latest_started_at=latest.started_at,
        latest_finished_at=latest.finished_at,
        latest_inserted_count=latest.inserted_count or 0,
        latest_skipped_count=latest.skipped_count or 0,
        latest_report_count=latest.report_count or 0,
        warnings=warnings,
        max_age_hours=max_age_hours,
    )
