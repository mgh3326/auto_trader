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


async def run_research_reports_ingest(
    *, payload_file: str, commit: bool = False,
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
        return {
            "status": "completed",
            "committed": False,
            "run_uuid": request.research_report_ingestion_run.run_uuid,
            "report_count": len(request.reports),
        }

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
