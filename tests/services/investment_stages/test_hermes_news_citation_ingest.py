# tests/services/investment_stages/test_hermes_news_citation_ingest.py
"""ROB-423 PR2 — Hermes composition news_citations → persisted rows (fail-open)."""

from __future__ import annotations

import decimal
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base
from app.models.investment_reports import InvestmentReport
from app.schemas.hermes_composition import (
    HermesCompositionIngestRequest,
    HermesCompositionResult,
    HermesNewsCitation,
)
from app.schemas.investment_reports import IngestReportItem
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository
from app.services.investment_stages.hermes_ingest import HermesCompositionIngestService


def _make_item(*, client_item_key: str, symbol: str) -> IngestReportItem:
    return IngestReportItem(
        client_item_key=client_item_key,
        item_kind="action",
        operation="review",
        symbol=symbol,
        side="buy",
        intent="buy_review",
        rationale="hermes rationale",
        apply_policy="requires_user_approval",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_composition_news_citations_persisted_and_unmatched_dropped(
    session: AsyncSession,
) -> None:
    # 2. Arrange: Seed a snapshot bundle with a news snapshot
    from app.schemas.investment_snapshots import (
        BundleCreate,
        BundleItemCreate,
        SnapshotCreate,
        SnapshotRunCreate,
    )
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    snapshots_repo = InvestmentSnapshotsRepository(session)

    run = await snapshots_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            requested_by="claude_code",
            policy_version="intraday_action_report_v1",
        )
    )

    bundle = await snapshots_repo.insert_bundle(
        BundleCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )
    
    # News payload mimicking PR1/collector payloads
    news_payload = {
        "articles": [
            {
                "title": "Apple Earnings",
                "url": "https://x/aapl-1",
                "source": "Reuters",
                "summary": "Apple outperforms",
                "published_at": "2026-05-05T12:00:00",
                "symbol": "AAPL",
                "provider": "finnhub",
                "external_article_id": "hash-aapl-1",
            }
        ],
        "fetch_records": [
            {
                "symbol": "AAPL",
                "provider": "finnhub",
                "requested_limit": 20,
                "returned_count": 1,
                "status": "ok",
                "error_code": None,
            }
        ],
        "market": "us",
    }
    
    # Insert news snapshot
    snap = await snapshots_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="news",
            market="us",
            account_scope=None,
            source_kind="news_ingestor",
            payload_json=news_payload,
            as_of=now,
            freshness_status="fresh",
        )
    )
    
    # Link snap to bundle
    await snapshots_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="optional"),
    )
    await session.commit()

    # 3. Hermes composition citing one real article and one bogus ref
    citation_real = HermesNewsCitation(
        external_article_id="hash-aapl-1",
        symbol="AAPL",
        relevance="direct",
        role="catalyst",
        decision_impact="strengthen_buy",
        selection_reason="great earnings",
        client_item_key="ci-1",
    )
    citation_bogus = HermesNewsCitation(
        external_article_id="does-not-exist",
        symbol="AAPL",
        relevance="direct",
        role="catalyst",
        decision_impact="strengthen_buy",
        selection_reason="bogus",
        client_item_key="ci-2",
    )

    composition = HermesCompositionResult(
        snapshot_bundle_uuid=bundle.bundle_uuid,
        hermes_run_id="run-1",
        title="Advisory Report",
        summary="Synthesized Advisory",
        items=[_make_item(client_item_key="ci-1", symbol="AAPL")],
        news_citations=[citation_real, citation_bogus],
    )

    request = HermesCompositionIngestRequest(
        composition=composition,
        kst_date="2026-05-23",
        market="us",
        account_scope="kis_live",
        status="draft",
    )

    # 4. Act: ingest the composition
    service = HermesCompositionIngestService(session)
    report = await service.ingest_composition(request)
    await session.commit()

    # 5. Assert:
    reports_repo = InvestmentReportsRepository(session)
    
    # Check citations written (only 1, real one)
    cites = await reports_repo.list_news_citations_for_report(report.report_uuid)
    assert len(cites) == 1
    c = cites[0]
    assert c.title == "Apple Earnings"
    assert c.canonical_url == "https://x/aapl-1"
    assert c.external_article_id == "hash-aapl-1"
    assert c.role == "catalyst"
    assert c.decision_impact == "strengthen_buy"
    assert c.relevance == "direct"
    assert c.confidence is None
    
    # Check bogus ref dropped and recorded in unavailable_sources
    refreshed_report = await reports_repo.get_report_by_id(report.id)
    assert refreshed_report.unavailable_sources == {
        "news_citations_unmatched": ["does-not-exist"]
    }

    # Check fetch_runs used_count tally
    fetch_runs_res = await session.execute(
        text("SELECT symbol, provider, used_count, returned_count FROM review.investment_report_news_fetch_runs WHERE report_uuid = :report_uuid"),
        {"report_uuid": report.report_uuid}
    )
    fetch_runs = fetch_runs_res.all()
    assert len(fetch_runs) == 1
    assert fetch_runs[0].symbol == "AAPL"
    assert fetch_runs[0].used_count == 1
    assert fetch_runs[0].returned_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_news_citations_is_noop(
    session: AsyncSession,
) -> None:
    # Seed bundle
    from app.schemas.investment_snapshots import BundleCreate
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    snapshots_repo = InvestmentSnapshotsRepository(session)
    bundle = await snapshots_repo.insert_bundle(
        BundleCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )
    await session.commit()

    composition = HermesCompositionResult(
        snapshot_bundle_uuid=bundle.bundle_uuid,
        hermes_run_id="run-2",
        title="Advisory Report Empty Cites",
        summary="Empty",
        items=[],
        news_citations=[],
    )

    request = HermesCompositionIngestRequest(
        composition=composition,
        kst_date="2026-05-23",
        market="us",
        account_scope="kis_live",
        status="draft",
    )

    service = HermesCompositionIngestService(session)
    report = await service.ingest_composition(request)
    await session.commit()

    reports_repo = InvestmentReportsRepository(session)
    cites = await reports_repo.list_news_citations_for_report(report.report_uuid)
    assert cites == []
