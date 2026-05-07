"""Ingest research-reports.v1 payload into research_reports / runs (ROB-140).

Pure ingestion: no broker / order / watch / scheduling side effects.
Schema-validated payload only — full text / pdf body are rejected upstream.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.research_reports import (
    ResearchReportIngestionRequest,
    ResearchReportIngestionResponse,
    ResearchReportPayloadV1,
)
from app.services.research_reports.repository import ResearchReportsRepository

logger = logging.getLogger(__name__)


def _payload_to_row(
    report: ResearchReportPayloadV1, *, ingestion_run_id: int | None
) -> dict:
    detail = report.detail
    pdf = report.pdf
    return {
        "dedup_key": report.dedup_key,
        "report_type": report.report_type,
        "source": report.source,
        "source_report_id": report.source_report_id,
        "title": report.title,
        "category": report.category,
        "analyst": report.analyst,
        "published_at_text": report.published_at_text,
        "published_at": report.published_at,
        "summary_text": report.summary_text,
        "detail_url": detail.url if detail else None,
        "detail_title": detail.title if detail else None,
        "detail_subtitle": detail.subtitle if detail else None,
        "detail_excerpt": detail.excerpt if detail else None,
        "pdf_url": pdf.url if pdf else None,
        "pdf_filename": pdf.filename if pdf else None,
        "pdf_sha256": pdf.sha256 if pdf else None,
        "pdf_size_bytes": pdf.size_bytes if pdf else None,
        "pdf_page_count": pdf.page_count if pdf else None,
        "pdf_text_length": pdf.text_length if pdf else None,
        "symbol_candidates": [sc.model_dump() for sc in report.symbol_candidates]
        if report.symbol_candidates
        else None,
        "raw_text_policy": report.raw_text_policy,
        "attribution_publisher": report.attribution.publisher,
        "attribution_copyright_notice": report.attribution.copyright_notice,
        "attribution_full_text_exported": report.attribution.full_text_exported,
        "attribution_pdf_body_exported": report.attribution.pdf_body_exported,
        "ingestion_run_id": ingestion_run_id,
    }


async def ingest_research_reports_v1(
    db: AsyncSession,
    request: ResearchReportIngestionRequest,
) -> ResearchReportIngestionResponse:
    repo = ResearchReportsRepository(db)
    run_meta = request.research_report_ingestion_run
    run = await repo.get_or_create_ingestion_run(
        run_uuid=run_meta.run_uuid,
        payload_version=run_meta.payload_version,
        source=run_meta.source,
        started_at=run_meta.started_at,
        finished_at=run_meta.finished_at,
        exported_at=run_meta.exported_at,
        report_count=run_meta.report_count,
        errors=run_meta.errors,
        flags=run_meta.flags,
        copyright_notice=run_meta.copyright_notice,
    )

    inserted = 0
    skipped = 0
    for report in request.reports:
        row_dict = _payload_to_row(report, ingestion_run_id=run.id)
        was_new = await repo.upsert_report(row_dict)
        if was_new:
            inserted += 1
        else:
            skipped += 1

    await repo.update_run_counts(run, inserted_count=inserted, skipped_count=skipped)

    return ResearchReportIngestionResponse(
        run_uuid=run.run_uuid,
        payload_version=run.payload_version,
        inserted_count=inserted,
        skipped_count=skipped,
        report_count=len(request.reports),
    )
