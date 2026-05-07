"""Query service tests (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete


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


async def _seed(
    db_session,
    dedup_key,
    *,
    source="naver_research",
    symbol="AAPL",
    published_at: datetime | None = None,
):
    from app.models.research_reports import ResearchReport

    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source=source,
        title=f"Title {dedup_key}",
        summary_text="summary",
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt="excerpt body",
        pdf_url=f"https://example.com/{dedup_key}.pdf",
        symbol_candidates=[{"symbol": symbol, "market": "us", "source": "t"}],
        attribution_publisher="naver_research",
        attribution_copyright_notice="© Naver",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=published_at or datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_symbol(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    await _seed(db_session, "a-1", symbol="AAPL")
    await _seed(db_session, "a-2", symbol="MSFT")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(symbol="AAPL")
    assert result.count == 1
    assert result.citations[0].title == "Title a-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_source(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    await _seed(db_session, "b-1", source="naver_research")
    await _seed(db_session, "b-2", source="kis_research")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(source="kis_research")
    assert result.count == 1
    assert result.citations[0].source == "kis_research"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_since(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    now = datetime.now(UTC)
    await _seed(db_session, "c-old", published_at=now - timedelta(days=30))
    await _seed(db_session, "c-new", published_at=now - timedelta(days=1))

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(since=now - timedelta(days=7))
    assert result.count == 1
    assert result.citations[0].title == "Title c-new"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_respects_limit(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    for i in range(5):
        await _seed(db_session, f"d-{i}")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(limit=3)
    assert result.count == 3
    assert len(result.citations) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_citations_never_include_full_body_field(db_session):
    """Read layer must never return any 'pdf_body' / 'full_text' / 'article_content' fields."""
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    await _seed(db_session, "e-1")
    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(symbol="AAPL")
    assert result.count == 1
    serialized = result.citations[0].model_dump()
    forbidden = {"pdf_body", "full_text", "article_content", "raw_payload"}
    assert forbidden.isdisjoint(serialized.keys()), (
        f"Forbidden body fields present: {set(serialized.keys()) & forbidden}"
    )
