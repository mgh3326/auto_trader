"""Query service tests (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


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

    source = f"test_symbol_filter_{uuid4()}"
    aapl_key = f"a-1-{uuid4()}"
    msft_key = f"a-2-{uuid4()}"
    await _seed(db_session, aapl_key, source=source, symbol="AAPL")
    await _seed(db_session, msft_key, source=source, symbol="MSFT")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(symbol="AAPL", source=source)
    assert result.count == 1
    assert result.citations[0].title == f"Title {aapl_key}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_source(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    kis_source = f"kis_research_{uuid4()}"
    await _seed(db_session, f"b-1-{uuid4()}", source=f"naver_research_{uuid4()}")
    await _seed(db_session, f"b-2-{uuid4()}", source=kis_source)

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(source=kis_source)
    assert result.count == 1
    assert result.citations[0].source == kis_source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_since(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    now = datetime.now(UTC)
    source = f"test_since_filter_{uuid4()}"
    old_key = f"c-old-{uuid4()}"
    new_key = f"c-new-{uuid4()}"
    await _seed(
        db_session, old_key, source=source, published_at=now - timedelta(days=30)
    )
    await _seed(
        db_session, new_key, source=source, published_at=now - timedelta(days=1)
    )

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(source=source, since=now - timedelta(days=7))
    assert result.count == 1
    assert result.citations[0].title == f"Title {new_key}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_respects_limit(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    source = f"test_limit_filter_{uuid4()}"
    for i in range(5):
        await _seed(db_session, f"d-{i}-{uuid4()}", source=source)

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(source=source, limit=3)
    assert result.count == 3
    assert len(result.citations) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_citations_never_include_full_body_field(db_session):
    """Read layer must never return any 'pdf_body' / 'full_text' / 'article_content' fields."""
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    source = f"test_no_body_fields_{uuid4()}"
    await _seed(db_session, f"e-1-{uuid4()}", source=source)
    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(symbol="AAPL", source=source)
    assert result.count == 1
    serialized = result.citations[0].model_dump()
    forbidden = {"pdf_body", "full_text", "article_content", "raw_payload"}
    assert forbidden.isdisjoint(serialized.keys()), (
        f"Forbidden body fields present: {set(serialized.keys()) & forbidden}"
    )
