"""ResearchReportsRepository tests (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean_research_reports(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


def _sample_report_dict(*, dedup_key: str = "abc-1") -> dict:
    return {
        "dedup_key": dedup_key,
        "report_type": "equity_research",
        "source": "naver_research",
        "source_report_id": "id-1",
        "title": "Apple Outlook",
        "category": "기업분석",
        "analyst": "김분석",
        "published_at_text": "2026-05-07 09:00",
        "published_at": datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
        "summary_text": "단기 약세, 장기 강세",
        "detail_url": "https://example.com/d/1",
        "detail_title": "Apple Outlook",
        "detail_subtitle": "long-term positive",
        "detail_excerpt": "buy, $220",
        "pdf_url": "https://example.com/x.pdf",
        "pdf_filename": "x.pdf",
        "pdf_sha256": "f" * 64,
        "pdf_size_bytes": 1024,
        "pdf_page_count": 10,
        "pdf_text_length": 8000,
        "symbol_candidates": [
            {"symbol": "AAPL", "market": "us", "source": "ticker_match"}
        ],
        "raw_text_policy": "metadata_only",
        "attribution_publisher": "naver_research",
        "attribution_copyright_notice": "© Naver",
        "attribution_full_text_exported": False,
        "attribution_pdf_body_exported": False,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_inserts_new_report(db_session):
    from app.models.research_reports import ResearchReport
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    inserted = await repo.upsert_report(_sample_report_dict(dedup_key="k-1"))
    await db_session.commit()
    assert inserted is True

    rows = (await db_session.execute(select(ResearchReport))).scalars().all()
    assert len(rows) == 1
    assert rows[0].dedup_key == "k-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_skips_duplicate_dedup_key(db_session):
    from app.models.research_reports import ResearchReport
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    inserted_first = await repo.upsert_report(_sample_report_dict(dedup_key="k-2"))
    inserted_second = await repo.upsert_report(_sample_report_dict(dedup_key="k-2"))
    await db_session.commit()

    assert inserted_first is True
    assert inserted_second is False

    rows = (await db_session.execute(select(ResearchReport))).scalars().all()
    assert len(rows) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_run_inserts_then_returns_existing(db_session):
    from app.models.research_reports import ResearchReportIngestionRun
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    run = await repo.get_or_create_ingestion_run(
        run_uuid="run-1",
        payload_version="research-reports.v1",
        source="naver_research",
        started_at=None,
        finished_at=None,
        exported_at=None,
        report_count=2,
        errors=None,
        flags=None,
        copyright_notice="© test",
    )
    await db_session.commit()
    assert run.id is not None
    first_id = run.id

    again = await repo.get_or_create_ingestion_run(
        run_uuid="run-1",
        payload_version="research-reports.v1",
        source="naver_research",
        started_at=None,
        finished_at=None,
        exported_at=None,
        report_count=2,
        errors=None,
        flags=None,
        copyright_notice="© test",
    )
    await db_session.commit()
    assert again.id == first_id

    rows = (
        (await db_session.execute(select(ResearchReportIngestionRun))).scalars().all()
    )
    assert len(rows) == 1
