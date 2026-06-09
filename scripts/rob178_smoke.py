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
import contextlib
import copy
import io
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

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
SMOKE_OUTPUT_DIR = (REPO_ROOT / ".smoke-out").resolve(strict=False)
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
    p.add_argument(
        "--apply",
        action="store_true",
        help="Execute service write-path ingestion. Default only validates and writes dry-run evidence.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly select the default no-write mode for operator checklists.",
    )
    return p.parse_args()


def _resolve_smoke_output_path(path: Path) -> Path:
    """Resolve evidence output and keep it in the repo-local .smoke-out/ tree."""
    candidate = path if path.is_absolute() else REPO_ROOT / path
    resolved = candidate.resolve(strict=False)
    try:
        relative_output = resolved.relative_to(SMOKE_OUTPUT_DIR)
    except ValueError as exc:
        raise SystemExit("--evidence must be under .smoke-out/") from exc
    if ".." in relative_output.parts:
        raise SystemExit("--evidence must not contain parent-directory traversal")
    return SMOKE_OUTPUT_DIR.joinpath(*relative_output.parts)


def _load_payload(path: Path) -> tuple[Path, dict]:
    if path.is_file() and path.stat().st_size > 0:
        return path, json.loads(path.read_text(encoding="utf-8"))
    logger.warning("payload missing or empty at %s; using pinned fixture", path)
    return FALLBACK_FIXTURE, json.loads(FALLBACK_FIXTURE.read_text(encoding="utf-8"))


def _parse_last_json_line(stdout: str) -> dict:
    """Return the last JSON object emitted by a CLI-style function."""
    for line in reversed(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError(f"operator CLI emitted no JSON object; stdout={stdout!r}")


def _run_operator_cli(payload_path: Path, *, dry_run: bool) -> dict:
    """Exercise the checked-in operator CLI entrypoint without shelling out."""
    from scripts.ingest_research_reports import main_async

    argv = ["--file", str(payload_path)]
    if dry_run:
        argv.append("--dry-run")
    logger.info("operator cli argv: %s", argv)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = asyncio.run(main_async(argv))
    output = stdout.getvalue()
    if exit_code != 0:
        raise RuntimeError(
            f"operator CLI failed with exit_code={exit_code}; stdout={output!r}"
        )
    return _parse_last_json_line(output)


async def _ingest_via_service(
    request: ResearchReportIngestionRequest,
) -> dict:
    """Commit one ingestion pass through the production service boundary."""
    async with AsyncSessionLocal() as db:
        try:
            response = await ingest_research_reports_v1(db, request)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return response.model_dump()


async def _read_back_via_service() -> dict:
    """Read back a compact citation sample through the query service."""
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
                "symbol_candidates": [sc.model_dump() for sc in c.symbol_candidates],
                "attribution_publisher": c.attribution_publisher,
                "attribution_copyright_notice": c.attribution_copyright_notice,
            }
        )
    return {"count": result.count, "citations_sample": citations}


async def _table_counts() -> dict:
    """Return DB-side row counts for the research report smoke tables."""
    async with AsyncSessionLocal() as db:
        reports = await db.scalar(select(func.count(ResearchReport.id)))
        runs = await db.scalar(select(func.count(ResearchReportIngestionRun.id)))
    return {
        "research_reports_rows": int(reports or 0),
        "research_report_ingestion_runs_rows": int(runs or 0),
    }


def _expect_full_text_rejection(payload: dict) -> dict:
    """Verify schema validation rejects explicit full-text export flags."""
    mutated = copy.deepcopy(payload)
    mutated["reports"][0]["attribution"]["full_text_exported"] = True
    try:
        ResearchReportIngestionRequest.model_validate(mutated)
    except Exception as exc:
        return {
            "rejected": True,
            "error_class": type(exc).__name__,
            "error_message": str(exc)[:200],
        }
    return {"rejected": False, "error_class": None, "error_message": None}


def _expect_forbidden_body_rejection(payload: dict) -> dict:
    """Verify schema validation rejects forbidden body-like fields."""
    mutated = copy.deepcopy(payload)
    mutated["reports"][0]["pdf_body"] = "this should be rejected"
    try:
        ResearchReportIngestionRequest.model_validate(mutated)
    except Exception as exc:
        return {
            "rejected": True,
            "error_class": type(exc).__name__,
            "error_message": str(exc)[:200],
        }
    return {"rejected": False, "error_class": None, "error_message": None}


def main() -> int:
    args = _parse_args()
    setup_logging_and_sentry(service_name="rob178_smoke")
    if "DATABASE_URL" not in os.environ:
        print(
            "DATABASE_URL must be set to a smoke-only DB url",
            file=sys.stderr,
        )
        return 2
    args.evidence = _resolve_smoke_output_path(args.evidence)
    payload_path, payload = _load_payload(args.payload)
    request = ResearchReportIngestionRequest.model_validate(payload)

    cli_dry = _run_operator_cli(payload_path, dry_run=True)
    logger.info("operator cli dry-run: %s", cli_dry)

    dry_run = not args.apply
    if dry_run:
        first = {"dry_run": True, "inserted_count": 0, "skipped_count": 0}
        second = {"dry_run": True, "inserted_count": 0, "skipped_count": 0}
        counts = asyncio.run(_table_counts())
        read_back = {"dry_run": True, "count": 0, "citations_sample": []}
    else:
        # ROB-469 PR2: run all DB work in ONE event loop. The production default DB
        # pool (AsyncAdaptedQueuePool) binds pooled connections to the loop that
        # created them, so separate asyncio.run() calls would hit "attached to a
        # different loop". A single asyncio.run keeps every checkout on one loop.
        async def _apply_smoke() -> tuple[dict, dict, dict, dict]:
            ingest_first = await _ingest_via_service(request)
            ingest_second = await _ingest_via_service(request)
            table_counts = await _table_counts()
            read = await _read_back_via_service()
            return ingest_first, ingest_second, table_counts, read

        first, second, counts, read_back = asyncio.run(_apply_smoke())

    full_text_check = _expect_full_text_rejection(payload)
    forbidden_body_check = _expect_forbidden_body_rejection(payload)

    evidence = {
        "smoke": "rob-178-research-reports-ingest",
        "captured_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
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
    invariants_ok = (
        bool(full_text_check.get("rejected"))
        and bool(forbidden_body_check.get("rejected"))
        and (
            dry_run
            or (
                first.get("inserted_count") == len(payload["reports"])
                and first.get("skipped_count") == 0
                and second.get("inserted_count") == 0
                and second.get("skipped_count") == len(payload["reports"])
                and read_back.get("count", 0) >= len(payload["reports"])
            )
        )
    )
    evidence["invariants_ok"] = invariants_ok

    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    # args.evidence is constrained to the repo-local .smoke-out/ tree by
    # _resolve_smoke_output_path(), and the evidence payload contains only
    # Pydantic-validated compact metadata.
    args.evidence.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False)
    )  # NOSONAR pythonsecurity:S2083
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    return 0 if invariants_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
