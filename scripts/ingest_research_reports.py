#!/usr/bin/env python3
"""Ingest a research-reports.v1 payload JSON file into auto_trader (ROB-140).

Usage:
    uv run python -m scripts.ingest_research_reports --file path/to/payload.json [--dry-run]

Reads the file, validates against ResearchReportIngestionRequest, and (unless dry-run)
upserts into research_reports / research_report_ingestion_runs. Prints a JSON summary.

Boundary: this is the only entry point that ingests news-ingestor output. Auto_trader
runtime never calls news-ingestor internals.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception, init_sentry
from app.schemas.research_reports import ResearchReportIngestionRequest
from app.services.research_reports.ingestion import ingest_research_reports_v1

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest a research-reports.v1 payload JSON file (ROB-140)."
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="Path to a research-reports.v1 JSON payload file.",
    )
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    return parser.parse_args(argv)


async def main_async(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="ingest-research-reports")
    ns = parse_args(argv)

    if not ns.file.is_file():
        logger.error("file not found: %s", ns.file)
        return 1

    raw = json.loads(ns.file.read_text(encoding="utf-8"))
    try:
        request = ResearchReportIngestionRequest.model_validate(raw)
    except Exception as exc:
        logger.error("payload validation failed: %s", exc)
        capture_exception(exc, process="ingest_research_reports")
        return 2

    if ns.dry_run:
        summary = {
            "dry_run": True,
            "run_uuid": request.research_report_ingestion_run.run_uuid,
            "report_count": len(request.reports),
        }
        print(json.dumps(summary))
        return 0

    async with AsyncSessionLocal() as db:
        try:
            response = await ingest_research_reports_v1(db, request)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.error("ingest failed: %s", exc, exc_info=True)
            capture_exception(exc, process="ingest_research_reports")
            return 3

    print(json.dumps(response.model_dump()))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
