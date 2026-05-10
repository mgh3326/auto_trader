#!/usr/bin/env python3
"""ROB-178 research_reports ingest/operations smoke driver.

Metadata-only end-to-end smoke. Does NOT mutate broker/order/watch state. Does
NOT activate any scheduler. Does NOT accept full PDF body or full extracted
text. Writes a compact evidence file to .smoke-out/evidence.json.

Run from the auto_trader worktree:

    DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \\
      uv run python scripts/rob178_smoke.py \\
        --payload .smoke-out/payload_live.json \\
        --evidence .smoke-out/evidence.json

If --payload is missing or the live file is empty, falls back to the pinned
fixture at tests/fixtures/rob178_payload_kis_truefriend_smoke.json.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.models.research_reports import (
    ResearchReport,
    ResearchReportIngestionRun,
)
from app.schemas.research_reports import ResearchReportIngestionRequest
from app.services.research_reports.ingestion import ingest_research_reports_v1
from app.services.research_reports.query_service import (
    ResearchReportsQueryService,
)

logger = logging.getLogger("rob178_smoke")

REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "rob178_payload_kis_truefriend_smoke.json"
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rob178_smoke")
    p.add_argument(
        "--payload",
        type=Path,
        default=Path(".smoke-out/payload_live.json"),
        help="Path to a research-reports.v1 payload JSON.",
    )
    p.add_argument(
        "--evidence",
        type=Path,
        default=Path(".smoke-out/evidence.json"),
        help="Where to write the smoke evidence summary.",
    )
    return p.parse_args()


def _load_payload(path: Path) -> tuple[Path, dict]:
    if path.is_file() and path.stat().st_size > 0:
        return path, json.loads(path.read_text(encoding="utf-8"))
    logger.warning("payload missing or empty at %s; using pinned fixture", path)
    return FALLBACK_FIXTURE, json.loads(FALLBACK_FIXTURE.read_text(encoding="utf-8"))


def _run_operator_cli(payload_path: Path, *, dry_run: bool) -> dict:
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "scripts.ingest_research_reports",
        "--file",
        str(payload_path),
    ]
    if dry_run:
        cmd.append("--dry-run")
    logger.info("operator cli: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, check=True, capture_output=True, text=True, cwd=REPO_ROOT
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


async def _ingest_via_service(
    request: ResearchReportIngestionRequest,
) -> dict:
    async with AsyncSessionLocal() as db:
        try:
            response = await ingest_research_reports_v1(db, request)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return response.model_dump()


async def _read_back_via_service() -> dict:
    async with AsyncSessionLocal() as db:
        svc = ResearchReportsQueryService(db)
        result = await svc.find_relevant(limit=20)
    citations = []
    for c in result.citations[:3]:
        citations.append(
            {
                "source": c.source,
                "title": c.title,
                "analyst": c.analyst,
                "published_at_text": c.published_at_text,
                "category": c.category,
                "detail_url": c.detail_url,
                "pdf_url": c.pdf_url,
                "excerpt_len": len(c.excerpt or ""),
                "symbol_candidates": [
                    sc.model_dump() for sc in c.symbol_candidates
                ],
                "attribution_publisher": c.attribution_publisher,
                "attribution_copyright_notice": c.attribution_copyright_notice,
            }
        )
    return {"count": result.count, "citations_sample": citations}


async def _table_counts() -> dict:
    async with AsyncSessionLocal() as db:
        reports = (await db.execute(select(ResearchReport))).scalars().all()
        runs = (
            await db.execute(select(ResearchReportIngestionRun))
        ).scalars().all()
    return {
        "research_reports_rows": len(reports),
        "research_report_ingestion_runs_rows": len(runs),
    }


def _expect_full_text_rejection(payload: dict) -> dict:
    mutated = copy.deepcopy(payload)
    mutated["reports"][0]["attribution"]["full_text_exported"] = True
    try:
        ResearchReportIngestionRequest.model_validate(mutated)
    except Exception as exc:
        return {"rejected": True, "error_class": type(exc).__name__,
                "error_message": str(exc)[:200]}
    return {"rejected": False, "error_class": None, "error_message": None}


def _expect_forbidden_body_rejection(payload: dict) -> dict:
    mutated = copy.deepcopy(payload)
    mutated["reports"][0]["pdf_body"] = "this should be rejected"
    try:
        ResearchReportIngestionRequest.model_validate(mutated)
    except Exception as exc:
        return {"rejected": True, "error_class": type(exc).__name__,
                "error_message": str(exc)[:200]}
    return {"rejected": False, "error_class": None, "error_message": None}


def main() -> int:
    setup_logging_and_sentry(service_name="rob178_smoke")
    if "DATABASE_URL" not in os.environ:
        print(
            "DATABASE_URL must be set to a smoke-only DB url",
            file=sys.stderr,
        )
        return 2
    args = _parse_args()
    payload_path, payload = _load_payload(args.payload)
    request = ResearchReportIngestionRequest.model_validate(payload)

    cli_dry = _run_operator_cli(payload_path, dry_run=True)
    logger.info("operator cli dry-run: %s", cli_dry)

    first = asyncio.run(_ingest_via_service(request))
    second = asyncio.run(_ingest_via_service(request))
    counts = asyncio.run(_table_counts())
    read_back = asyncio.run(_read_back_via_service())

    full_text_check = _expect_full_text_rejection(payload)
    forbidden_body_check = _expect_forbidden_body_rejection(payload)

    evidence = {
        "smoke": "rob-178-research-reports-ingest",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "payload_path": str(payload_path),
        "payload_run_uuid": payload["research_report_ingestion_run"]["run_uuid"],
        "payload_report_count": len(payload["reports"]),
        "operator_cli_dry_run": cli_dry,
        "first_ingest": first,
        "second_ingest_idempotent": second,
        "table_counts_after": counts,
        "read_back_via_service": read_back,
        "guardrails": {
            "full_text_exported_rejected": full_text_check,
            "forbidden_body_field_rejected": forbidden_body_check,
        },
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, ensure_ascii=False))
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
