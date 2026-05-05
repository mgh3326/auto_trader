import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from app.mcp_server.tooling.research_pipeline_read import (
    research_session_get_impl,
    research_session_list_recent_impl,
    stage_analysis_get_impl,
    research_summary_get_impl,
)
from app.models.research_pipeline import ResearchSession, StageAnalysis, ResearchSummary

@pytest.mark.asyncio
async def test_research_session_get_impl_success():
    mock_session = MagicMock(spec=ResearchSession)
    mock_session.id = 1
    mock_session.stock_info_id = 10
    mock_session.research_run_id = 100
    mock_session.status = "finalized"
    mock_session.started_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_session.finalized_at = datetime(2023, 1, 1, 0, 10, tzinfo=timezone.utc)
    mock_session.created_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_session.updated_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    
    mock_stage = MagicMock(spec=StageAnalysis)
    mock_stage.id = 1
    mock_stage.stage_type = "market"
    mock_stage.verdict = "bull"
    mock_stage.confidence = 70
    mock_stage.signals = {"price": 100}
    mock_stage.executed_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    
    mock_summary = MagicMock(spec=ResearchSummary)
    mock_summary.id = 1
    mock_summary.decision = "buy"
    mock_summary.confidence = 80
    mock_summary.reasons = ["Reason"]
    mock_summary.detailed_text = "Detailed"
    mock_summary.executed_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_summary.stage_links = []
    
    mock_session.stage_analyses = [mock_stage]
    mock_session.summaries = [mock_summary]
    
    with patch("app.mcp_server.tooling.research_pipeline_read.AsyncSessionLocal") as mock_db_factory:
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_session
        mock_db.execute.return_value = mock_result
        
        result = await research_session_get_impl(1)
        
        assert result["id"] == 1
        assert result["status"] == "finalized"
        assert len(result["stage_analyses"]) == 1
        assert result["stage_analyses"][0]["stage_type"] == "market"
        assert len(result["summaries"]) == 1

@pytest.mark.asyncio
async def test_research_session_get_impl_not_found():
    with patch("app.mcp_server.tooling.research_pipeline_read.AsyncSessionLocal") as mock_db_factory:
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        
        result = await research_session_get_impl(999)
        assert "error" in result
        assert result["error_type"] == "not_found"

@pytest.mark.asyncio
async def test_research_session_list_recent_impl():
    mock_session = MagicMock(spec=ResearchSession)
    mock_session.id = 1
    mock_session.stock_info_id = 10
    mock_session.status = "finalized"
    mock_session.created_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    
    mock_summary = MagicMock(spec=ResearchSummary)
    mock_summary.decision = "buy"
    mock_summary.confidence = 80
    mock_summary.executed_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    
    mock_session.summaries = [mock_summary]
    
    with patch("app.mcp_server.tooling.research_pipeline_read.AsyncSessionLocal") as mock_db_factory:
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_session]
        mock_db.execute.return_value = mock_result
        
        result = await research_session_list_recent_impl(limit=5)
        
        assert "sessions" in result
        assert len(result["sessions"]) == 1
        assert result["sessions"][0]["decision"] == "buy"

@pytest.mark.asyncio
async def test_stage_analysis_get_impl():
    mock_stage = MagicMock(spec=StageAnalysis)
    mock_stage.id = 1
    mock_stage.session_id = 1
    mock_stage.stage_type = "market"
    mock_stage.verdict = "bull"
    mock_stage.confidence = 70
    mock_stage.signals = {"price": 100}
    mock_stage.raw_payload = {}
    mock_stage.source_freshness = {}
    mock_stage.model_name = "gpt-4"
    mock_stage.prompt_version = "1.0"
    mock_stage.snapshot_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_stage.executed_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    
    with patch("app.mcp_server.tooling.research_pipeline_read.AsyncSessionLocal") as mock_db_factory:
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_stage
        mock_db.execute.return_value = mock_result
        
        result = await stage_analysis_get_impl(1)
        
        assert result["id"] == 1
        assert result["stage_type"] == "market"
        assert result["model_name"] == "gpt-4"

@pytest.mark.asyncio
async def test_research_summary_get_impl():
    mock_summary = MagicMock(spec=ResearchSummary)
    mock_summary.id = 1
    mock_summary.session_id = 1
    mock_summary.decision = "buy"
    mock_summary.confidence = 80
    mock_summary.bull_arguments = []
    mock_summary.bear_arguments = []
    mock_summary.price_analysis = {}
    mock_summary.reasons = ["Reason"]
    mock_summary.detailed_text = "Detailed"
    mock_summary.warnings = []
    mock_summary.model_name = "gpt-4"
    mock_summary.prompt_version = "1.0"
    mock_summary.executed_at = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_summary.stage_links = []
    
    with patch("app.mcp_server.tooling.research_pipeline_read.AsyncSessionLocal") as mock_db_factory:
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_summary
        mock_db.execute.return_value = mock_result
        
        result = await research_summary_get_impl(1)
        
        assert result["id"] == 1
        assert result["decision"] == "buy"
        assert "links" in result
