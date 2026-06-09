# tests/services/investment_reports/test_bundle_news_citations.py
"""ROB-423 PR2 — detail bundle exposes news_citations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.query_service import InvestmentReportQueryService
from app.services.investment_reports.repository import InvestmentReportsRepository


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_bundle_includes_news_citations(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)

    # 1. Seed a report row
    report = await repo.insert_report(
        idempotency_key="rob423:detail:1",
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="HERMES_ADVISOR",
        title="baseline report",
        summary="s",
        status="draft",
        report_metadata={},
        market_snapshot={},
        portfolio_snapshot={},
    )
    await session.commit()

    # 2. Seed a citation row via repo
    await repo.insert_news_citation(
        report_uuid=report.report_uuid,
        market="us",
        symbol="AAPL",
        provider="finnhub",
        external_article_id="ext-1",
        canonical_url="https://x/1",
        title="AAPL News",
        relevance="direct",
        role="catalyst",
        decision_impact="strengthen_buy",
        fetched_at=datetime.now(tz=UTC),
    )
    await session.commit()

    # 3. Act: query bundle
    query_service = InvestmentReportQueryService(session)
    bundle = await query_service.get_bundle(report.report_uuid)

    # 4. Assert:
    assert bundle is not None
    assert "news_citations" in bundle
    cites = bundle["news_citations"]
    assert len(cites) == 1
    c = cites[0]
    assert c.title == "AAPL News"
    assert c.symbol == "AAPL"
    assert c.canonical_url == "https://x/1"
    assert c.role == "catalyst"
