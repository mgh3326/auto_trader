import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)
from app.services.action_report.snapshot_backed.request import ReportGenerationRequest


@pytest.mark.asyncio
async def test_staged_pipeline_e2e_with_mocked_llm(db_session):
    # This integration test verifies that setting auto_compose=True
    # triggers the StageRunner and FinalComposer.

    # 1. Mock the ensure_service to return a bundle with snapshots
    bundle_uuid = uuid.uuid4()
    ensure_mock = AsyncMock()
    from types import SimpleNamespace
    ensure_mock.ensure.return_value = SimpleNamespace(
        bundle_uuid=bundle_uuid,
        status="complete",
        coverage_summary={},
        freshness_summary={"overall": "fresh"},
        missing_sources=[],
        warnings=[],
        created=True,
    )

    # 2. Mock GeminiProvider.ask to return consistent JSON for stages and composer
    with patch("app.services.ai_providers.gemini_provider.GeminiProvider.ask") as mock_ask:
        from app.services.ai_providers.base import AiProviderResult

        # We expect 4 LLM calls (bull, bear, risk, composer)
        mock_ask.side_effect = [
            # bull_reducer
            AiProviderResult(answer='{"verdict": "bull", "confidence": 70, "summary": "Bull synthesis"}', provider="g", model="m", usage=None, elapsed_ms=1),
            # bear_reducer
            AiProviderResult(answer='{"verdict": "bear", "confidence": 30, "summary": "Bear synthesis"}', provider="g", model="m", usage=None, elapsed_ms=1),
            # risk_review
            AiProviderResult(answer='{"verdict": "bull", "confidence": 60, "summary": "Risk review"}', provider="g", model="m", usage=None, elapsed_ms=1),
            # final_composer
            AiProviderResult(answer='{"title": "AI Report", "summary": "Full summary", "items": [{"client_item_key": "x", "item_kind": "action", "intent": "buy_review", "symbol": "BTC", "side": "buy", "rationale": "r", "cited_stage_types": ["bull_reducer"]}]}', provider="g", model="m", usage=None, elapsed_ms=1),
        ]

        generator = SnapshotBackedReportGenerator(
            db_session,
            ensure_service=ensure_mock,
        )

        request = ReportGenerationRequest(
            market="crypto",
            account_scope="upbit_live",
            created_by_profile="AI_ADVISOR",
            title="DUMMY",
            summary="DUMMY",
            kst_date="2026-05-20",
            auto_compose=True,
        )

        # We need a real bundle in the DB because _LocalBundleRead fetches it
        from app.models.investment_snapshots import InvestmentSnapshotBundle
        bundle = InvestmentSnapshotBundle(
            bundle_uuid=bundle_uuid,
            purpose="report_generation",
            market="crypto",
            account_scope="upbit_live",
            status="complete",
            policy_version="v1",
            as_of=datetime.now(tz=UTC),
            idempotency_key=f"e2e-test-{bundle_uuid.hex[:12]}",
        )
        db_session.add(bundle)
        await db_session.flush()

        response = await generator.generate(request)

        assert response.bundle_status == "complete"
        assert response.items_count == 1

        # Verify ingestion occurred via side effects or by checking DB
        from sqlalchemy import select

        from app.models.investment_reports import InvestmentReport
        result = await db_session.execute(select(InvestmentReport).where(InvestmentReport.report_uuid == response.report_uuid))
        report = result.scalar_one()
        assert report.title == "AI Report"
        assert "investment_stage_run_uuid" in report.report_metadata

        # Verify stage run was persisted
        from app.models.investment_stages import InvestmentStageRun
        run_uuid = uuid.UUID(report.report_metadata["investment_stage_run_uuid"])
        result = await db_session.execute(select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid))
        stage_run = result.scalar_one()
        assert stage_run.status == "completed"
        assert len(stage_run.artifacts) == 8
