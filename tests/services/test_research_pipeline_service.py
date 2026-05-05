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
