"""ROB-207 job-runner boundary tests."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from uuid import uuid4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_research_reports_ingest_dry_run_returns_counts(tmp_path: Path):
    from app.jobs.research_reports_ingest import run_research_reports_ingest

    payload = {
        "research_report_ingestion_run": {
            "run_uuid": f"run-{uuid4()}",
            "payload_version": "research-reports.v1",
            "source": "naver_research",
            "report_count": 1,
        },
        "reports": [
            {
                "dedup_key": f"k-job-{uuid4()}",
                "report_type": "equity_research",
                "source": "naver_research",
                "attribution": {"full_text_exported": False, "pdf_body_exported": False},
            }
        ],
    }
    payload_file = tmp_path / "p.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    result = await run_research_reports_ingest(
        payload_file=str(payload_file), commit=False,
    )
    assert result["status"] == "completed"
    assert result["committed"] is False
    assert result["report_count"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_research_reports_ingest_missing_file_returns_failed_status():
    from app.jobs.research_reports_ingest import run_research_reports_ingest

    result = await run_research_reports_ingest(
        payload_file="/nonexistent/path.json", commit=False,
    )
    assert result["status"] == "failed"
    assert "file" in result["error"].lower() or "not found" in result["error"].lower()
