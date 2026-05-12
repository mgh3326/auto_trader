"""ROB-207 TaskIQ task — manual / Prefect-triggered dry-run smoke for the ingest bridge.

NO recurring schedule. Production activation is approval-gated and lives in the
out-of-repo Prefect deployment, not here. Defaults to dry-run (commit=False).
"""

from __future__ import annotations

import logging
import os

from app.core.taskiq_broker import broker
from app.jobs.research_reports_ingest import run_research_reports_ingest

logger = logging.getLogger(__name__)


def _default_payload_file() -> str:
    return os.environ.get("RESEARCH_REPORTS_INGEST_PAYLOAD_FILE", "")


@broker.task(task_name="research_reports.ingest_bulk_smoke")
async def research_reports_ingest_bulk_smoke(
    payload_file: str | None = None,
    commit: bool = False,
) -> dict:
    target = payload_file or _default_payload_file()
    if not target:
        return {
            "status": "failed",
            "error": "payload_file not provided",
            "committed": False,
        }
    return await run_research_reports_ingest(payload_file=target, commit=commit)
