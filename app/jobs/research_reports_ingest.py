"""ROB-207 job-runner boundary for research_reports ingest.

Wraps ingest_research_reports_v1 with a structured pass/fail result so TaskIQ tasks
and the diagnose CLI can share the same boundary. Never raises on operational errors.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.db import AsyncSessionLocal
from app.schemas.research_reports import ResearchReportIngestionRequest
from app.services.research_reports.ingestion import ingest_research_reports_v1

logger = logging.getLogger(__name__)


def preview_research_reports_payload(request: ResearchReportIngestionRequest) -> dict:
    """Return dry-run evidence for an ingest payload without DB writes.

    Keep this intentionally citation-metadata-only: no summary/body/excerpt fields.
    """
    return {
        "status": "completed",
        "committed": False,
        "run_uuid": request.research_report_ingestion_run.run_uuid,
        "report_count": len(request.reports),
        "dedup_keys": [report.dedup_key for report in request.reports],
        "citation_metadata": [
            {
                "dedup_key": report.dedup_key,
                "source": report.source,
                "title": report.title,
                "category": report.category,
                "analyst": report.analyst,
                "published_at_text": report.published_at_text,
                "published_at": report.published_at.isoformat()
                if report.published_at
                else None,
                "detail_url": report.detail.url if report.detail else None,
                "pdf_url": report.pdf.url if report.pdf else None,
                "symbol_candidates": [
                    candidate.model_dump(mode="json")
                    for candidate in report.symbol_candidates
                ],
            }
            for report in request.reports
        ],
    }


async def run_research_reports_ingest(
    *,
    payload_file: str,
    commit: bool = False,
) -> dict:
    path = Path(payload_file)
    if not path.is_file():
        return {
            "status": "failed",
            "error": f"payload file not found: {payload_file}",
            "committed": False,
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        request = ResearchReportIngestionRequest.model_validate(raw)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"payload validation failed: {exc}",
            "committed": False,
        }

    if not commit:
        return preview_research_reports_payload(request)

    async with AsyncSessionLocal() as db:
        try:
            response = await ingest_research_reports_v1(db, request)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.exception("research_reports ingest commit failed")
            return {
                "status": "failed",
                "error": str(exc)[:500],
                "committed": False,
            }

    return {
        "status": "completed",
        "committed": True,
        "run_uuid": response.run_uuid,
        "inserted_count": response.inserted_count,
        "skipped_count": response.skipped_count,
        "report_count": response.report_count,
    }
