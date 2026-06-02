# tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py
"""ROB-423 PR2 — mock_preview copies live news citations (no re-fetch)."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock

from app.models.base import Base
from app.models.investment_reports import InvestmentReport
from app.services.investment_reports.mock_preview.runner import MockPreviewReportRunner
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository
from app.schemas.investment_snapshots import BundleCreate

@pytest.mark.integration
@pytest.mark.asyncio
async def test_mock_preview_copies_live_citations(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    snapshots_repo = InvestmentSnapshotsRepository(session)

    now = datetime.now(tz=timezone.utc)

    # Seed live bundle
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

    # 2. Seed a live report with an item and news citation
    live = await repo.insert_report(
        idempotency_key="rob423:mockcopy:live",
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="HERMES_ADVISOR",
        title="live report",
        summary="s",
        status="draft",
        report_metadata={},
        market_snapshot={},
        portfolio_snapshot={},
        snapshot_bundle_uuid=bundle.bundle_uuid,
    )
    await session.commit()

    # Add item
    item = await repo.insert_item(
        report_id=live.id,
        item_uuid=uuid.uuid4(),
        idempotency_key="rob423:mockcopy:item1",
        item_kind="action",
        operation="review",
        symbol="AAPL",
        side="buy",
        intent="buy_review",
        rationale="r",
        apply_policy="requires_user_approval",
    )
    await session.commit()

    # Add 2 news citations
    citation1 = await repo.insert_news_citation(
        report_uuid=live.report_uuid,
        report_item_uuid=item.item_uuid,
        market="us",
        symbol="AAPL",
        provider="finnhub",
        external_article_id="ext-aapl-1",
        canonical_url="https://x/aapl-1",
        title="Apple Catalyst",
        relevance="direct",
        role="catalyst",
        decision_impact="strengthen_buy",
        fetched_at=datetime.now(tz=timezone.utc),
    )
    citation2 = await repo.insert_news_citation(
        report_uuid=live.report_uuid,
        report_item_uuid=None,
        market="us",
        symbol="MSFT",
        provider="finnhub",
        external_article_id="ext-msft-1",
        canonical_url="https://x/msft-1",
        title="MSFT Related",
        relevance="related",
        role="confirmation",
        decision_impact="hold_watch",
        fetched_at=datetime.now(tz=timezone.utc),
    )
    await session.commit()

    # Mock ensure service so it doesn't do real external snapshot logic
    ensure_mock = AsyncMock()
    ensure_mock.ensure_reusing_account_independent = AsyncMock(
        return_value=AsyncMock(bundle_uuid=uuid.uuid4())
    )

    runner = MockPreviewReportRunner(
        session=session,
        ensure_service=ensure_mock,
    )

    # 3. Act: run MockPreviewReportRunner
    mock_report, reused, count = await runner.run(
        live_report_uuid=live.report_uuid,
        market="us",
        market_session=None,
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-23",
        created_by_profile="HERMES_ADVISOR",
    )
    await session.commit()

    # 4. Assert: mock report carries copied citations
    assert mock_report is not None
    assert mock_report.report_uuid != live.report_uuid

    mock_cites = await repo.list_news_citations_for_report(mock_report.report_uuid)
    assert len(mock_cites) == 2
    
    c1 = next(c for c in mock_cites if c.symbol == "AAPL")
    assert c1.title == "Apple Catalyst"
    # report_item_uuid is NULL on copies!
    assert c1.report_item_uuid is None
    assert c1.metadata_json == {"copied_from_report_uuid": str(live.report_uuid)}

    c2 = next(c for c in mock_cites if c.symbol == "MSFT")
    assert c2.title == "MSFT Related"
    assert c2.report_item_uuid is None
    assert c2.metadata_json == {"copied_from_report_uuid": str(live.report_uuid)}
