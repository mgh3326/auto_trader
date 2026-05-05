"""Service tests for ROB-113 additions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.research_pipeline_service import ResearchPipelineService


@pytest.mark.asyncio
async def test_create_session_and_dispatch_returns_session_id_without_awaiting_run():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    fake_session = MagicMock()
    fake_session.id = 99
    fake_session.status = "running"
    fake_session.started_at = datetime.now(UTC)

    with (
        patch(
            "app.services.research_pipeline_service.create_stock_if_not_exists",
            new_callable=AsyncMock,
        ) as fake_create_stock,
        patch(
            "app.services.research_pipeline_service.ResearchSession",
            return_value=fake_session,
        ),
        patch(
            "app.services.research_pipeline_service.asyncio.create_task"
        ) as fake_create_task,
    ):
        fake_create_stock.return_value = MagicMock(id=1)
        service = ResearchPipelineService(db)

        result = await service.create_session_and_dispatch(
            symbol="KRW-BTC",
            name="Bitcoin",
            instrument_type="crypto",
            research_run_id=None,
            user_id=None,
        )

        assert result.session_id == 99
        assert result.status in {"running", "open"}
        assert fake_create_task.called, "stage execution must be dispatched async"


@pytest.mark.asyncio
async def test_get_latest_summary_includes_summary_stage_links(monkeypatch):
    from datetime import UTC, datetime
    from app.models.research_pipeline import (
        ResearchSummary,
        StageAnalysis,
        SummaryStageLink,
    )

    fake_link = MagicMock(spec=SummaryStageLink)
    fake_link.stage_analysis_id = 7
    fake_link.direction = "support"
    fake_link.weight = 0.8
    fake_link.rationale = "rsi oversold"

    fake_stage = MagicMock(spec=StageAnalysis)
    fake_stage.id = 7
    fake_stage.stage_type = "market"

    fake_summary = MagicMock(spec=ResearchSummary)
    fake_summary.id = 1
    fake_summary.session_id = 10
    fake_summary.decision = "buy"
    fake_summary.confidence = 80
    fake_summary.bull_arguments = []
    fake_summary.bear_arguments = []
    fake_summary.price_analysis = None
    fake_summary.reasons = None
    fake_summary.detailed_text = None
    fake_summary.warnings = None
    fake_summary.executed_at = datetime.now(UTC)
    fake_summary.stage_links = [fake_link]

    db = MagicMock()
    summary_result = MagicMock()
    summary_result.scalar_one_or_none.return_value = fake_summary

    stage_result = MagicMock()
    stage_result.scalars.return_value.all.return_value = [fake_stage]

    db.execute = AsyncMock(side_effect=[summary_result, stage_result])

    service = ResearchPipelineService(db)
    result = await service.get_latest_summary(10)

    assert result is not None
    assert result["summary_stage_links"] == [
        {
            "stage_analysis_id": 7,
            "stage_type": "market",
            "direction": "support",
            "weight": 0.8,
            "rationale": "rsi oversold",
        }
    ]
