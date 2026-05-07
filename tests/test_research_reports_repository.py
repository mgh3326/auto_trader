"""ResearchReportsRepository tests (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select


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
    dedup_key = f"k-1-{uuid4()}"
    inserted = await repo.upsert_report(_sample_report_dict(dedup_key=dedup_key))
    await db_session.commit()
    assert inserted is True

    rows = (
        (
            await db_session.execute(
                select(ResearchReport).where(ResearchReport.dedup_key == dedup_key)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].dedup_key == dedup_key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_skips_duplicate_dedup_key(db_session):
    from app.models.research_reports import ResearchReport
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    dedup_key = f"k-2-{uuid4()}"
    inserted_first = await repo.upsert_report(_sample_report_dict(dedup_key=dedup_key))
    inserted_second = await repo.upsert_report(_sample_report_dict(dedup_key=dedup_key))
    await db_session.commit()

    assert inserted_first is True
    assert inserted_second is False

    rows = (
        (
            await db_session.execute(
                select(ResearchReport).where(ResearchReport.dedup_key == dedup_key)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_run_inserts_then_returns_existing(db_session):
    from app.models.research_reports import ResearchReportIngestionRun
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    run_uuid = f"run-{uuid4()}"
    run = await repo.get_or_create_ingestion_run(
        run_uuid=run_uuid,
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
        run_uuid=run_uuid,
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
        (
            await db_session.execute(
                select(ResearchReportIngestionRun).where(
                    ResearchReportIngestionRun.run_uuid == run_uuid
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
