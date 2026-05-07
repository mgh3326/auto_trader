"""Ingestion service tests (ROB-140)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


def _sample_payload(*, dedup_keys: list[str] | None = None) -> dict:
    keys = dedup_keys or ["k-A"]
    reports = []
    for k in keys:
        reports.append(
            {
                "dedup_key": k,
                "report_type": "equity_research",
                "source": "naver_research",
                "title": f"Title {k}",
                "summary_text": "summary",
                "detail": {
                    "url": f"https://example.com/{k}",
                    "excerpt": "excerpt",
                },
                "pdf": {
                    "url": f"https://example.com/{k}.pdf",
                    "sha256": "f" * 64,
                    "page_count": 10,
                    "text_length": 5000,
                },
                "symbol_candidates": [
                    {"symbol": "AAPL", "market": "us", "source": "ticker"}
                ],
                "raw_text_policy": "metadata_only",
                "attribution": {
                    "publisher": "naver_research",
                    "copyright_notice": "© Naver",
                    "full_text_exported": False,
                    "pdf_body_exported": False,
                },
            }
        )
    return {
        "research_report_ingestion_run": {
            "run_uuid": "run-1",
            "payload_version": "research-reports.v1",
            "source": "naver_research",
            "report_count": len(reports),
        },
        "reports": reports,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_inserts_reports_and_run(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )
    from app.schemas.research_reports import ResearchReportIngestionRequest
    from app.services.research_reports.ingestion import ingest_research_reports_v1

    req = ResearchReportIngestionRequest.model_validate(
        _sample_payload(dedup_keys=["k-A", "k-B"])
    )
    response = await ingest_research_reports_v1(db_session, req)
    await db_session.commit()

    assert response.inserted_count == 2
    assert response.skipped_count == 0

    reports = (await db_session.execute(select(ResearchReport))).scalars().all()
    assert {r.dedup_key for r in reports} == {"k-A", "k-B"}

    runs = (
        (await db_session.execute(select(ResearchReportIngestionRun))).scalars().all()
    )
    assert len(runs) == 1
    assert runs[0].inserted_count == 2
    assert runs[0].skipped_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_is_idempotent_on_duplicate(db_session):
    from app.schemas.research_reports import ResearchReportIngestionRequest
    from app.services.research_reports.ingestion import ingest_research_reports_v1

    req = ResearchReportIngestionRequest.model_validate(
        _sample_payload(dedup_keys=["k-X"])
    )

    first = await ingest_research_reports_v1(db_session, req)
    await db_session.commit()
    second = await ingest_research_reports_v1(db_session, req)
    await db_session.commit()

    assert first.inserted_count == 1
    assert first.skipped_count == 0
    assert second.inserted_count == 0
    assert second.skipped_count == 1


@pytest.mark.unit
def test_ingest_request_rejects_full_text_exported():
    """Schema-level guard: ingestion never sees a payload with full body."""
    from app.schemas.research_reports import ResearchReportIngestionRequest

    payload = _sample_payload(dedup_keys=["k-bad"])
    payload["reports"][0]["attribution"]["full_text_exported"] = True

    with pytest.raises(ValidationError):
        ResearchReportIngestionRequest.model_validate(payload)
