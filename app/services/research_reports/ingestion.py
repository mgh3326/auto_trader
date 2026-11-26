"""Ingest research-reports.v1 payload into research_reports / runs (ROB-140).

Pure ingestion: no broker / order / watch / scheduling side effects.
Schema-validated payload only — full text / pdf body are rejected upstream.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

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
    row = {
        "dedup_key": report.dedup_key,
        "report_type": report.report_type,
        "source": report.source,
        "ingestion_run_id": ingestion_run_id,
    }
    optional_fields = (
        "source_report_id",
        "title",
        "category",
        "analyst",
        "published_at_text",
        "published_date",
        "published_at",
        "summary_text",
        "raw_text_policy",
    )
    for name in optional_fields:
        if name in report.model_fields_set:
            row[name] = getattr(report, name)

    for input_name, output_prefix, field_names in (
        (
            "detail",
            "detail",
            ("url", "title", "subtitle", "excerpt"),
        ),
        (
            "pdf",
            "pdf",
            ("url", "filename", "sha256", "size_bytes", "page_count", "text_length"),
        ),
    ):
        if input_name not in report.model_fields_set:
            continue
        nested = getattr(report, input_name)
        for name in field_names:
            if nested is None or name in nested.model_fields_set:
                row[f"{output_prefix}_{name}"] = (
                    getattr(nested, name) if nested is not None else None
                )

    if "symbol_candidates" in report.model_fields_set:
        row["symbol_candidates"] = (
            [candidate.model_dump() for candidate in report.symbol_candidates]
            if report.symbol_candidates
            else None
        )
    if "attribution" in report.model_fields_set:
        attribution_fields = {
            "publisher": "attribution_publisher",
            "copyright_notice": "attribution_copyright_notice",
            "full_text_exported": "attribution_full_text_exported",
            "pdf_body_exported": "attribution_pdf_body_exported",
        }
        for input_name, output_name in attribution_fields.items():
            if input_name in report.attribution.model_fields_set:
                row[output_name] = getattr(report.attribution, input_name)
    return row


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

    errors: list[dict] = []
    inserted = 0
    skipped = 0
    for report in request.reports:
        row_dict = _payload_to_row(report, ingestion_run_id=run.id)
        try:
            was_new = await repo.upsert_report(row_dict)
        except Exception as exc:
            errors.append({"dedup_key": report.dedup_key, "error": str(exc)[:500]})
            logger.warning(
                "research_reports: per-report ingest failed dedup_key=%s err=%s",
                report.dedup_key,
                exc,
            )
            continue
        if was_new:
            inserted += 1
        else:
            skipped += 1

    await repo.update_run_counts(run, inserted_count=inserted, skipped_count=skipped)
    await repo.mark_run_finished(
        run,
        finished_at=datetime.now(UTC),
        errors=errors or None,
    )

    return ResearchReportIngestionResponse(
        run_uuid=run.run_uuid,
        payload_version=run.payload_version,
        inserted_count=inserted,
        skipped_count=skipped,
        report_count=len(request.reports),
    )
