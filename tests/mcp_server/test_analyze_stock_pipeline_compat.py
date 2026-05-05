from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_analyze import analyze_stock_impl
from app.models.research_pipeline import ResearchSession, ResearchSummary, StageAnalysis


@pytest.fixture
def mock_ohlcv_df():
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        },
        index=[pd.Timestamp.now()],
    )


@pytest.mark.asyncio
async def test_analyze_stock_pipeline_compat_success():
    """Verify analyze_stock_impl uses pipeline and returns compatible structure."""

    mock_session_id = 123

    # We need real or realistic MagicMocks that can be used by _map_pipeline_to_analysis
    mock_summary = MagicMock(spec=ResearchSummary)
    mock_summary.decision = MagicMock()
    mock_summary.decision.value = "buy"
    mock_summary.confidence = 80
    mock_summary.reasons = ["Reason 1", "Reason 2"]
    mock_summary.detailed_text = "Detailed reasoning"
    mock_summary.bull_arguments = ["Bull 1"]
    mock_summary.bear_arguments = ["Bear 1"]
    mock_summary.price_analysis = {
        "appropriate_buy_min": 100,
        "appropriate_buy_max": 110,
        "appropriate_sell_min": 150,
        "appropriate_sell_max": 160,
    }
    mock_summary.warnings = []
    mock_summary.model_name = "test-model"
    mock_summary.prompt_version = "1.0"
    mock_summary.executed_at = datetime.now(UTC)

    mock_stage = MagicMock(spec=StageAnalysis)
    mock_stage.stage_type = "market"
    mock_stage.signals = {
        "last_close": 105.0,
        "change_pct": 1.5,
    }

    mock_session = MagicMock(spec=ResearchSession)
    mock_session.id = mock_session_id
    mock_session.summaries = [mock_summary]
    mock_session.stage_analyses = [mock_stage]

    with patch("app.mcp_server.tooling.analysis_analyze.settings") as mock_settings:
        mock_settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED = True
        mock_settings.RESEARCH_PIPELINE_ENABLED = True

        # Patch the source module since it's imported locally in analyze_stock_impl
        with patch(
            "app.analysis.pipeline.run_research_session", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = mock_session_id

            with patch(
                "app.mcp_server.tooling.analysis_analyze.AsyncSessionLocal"
            ) as mock_db_factory:
                mock_db = AsyncMock()
                mock_db_factory.return_value.__aenter__.return_value = mock_db

                # Mock result for _get_pipeline_result's query
                mock_result = MagicMock()
                mock_result.scalar_one_or_none.return_value = mock_session
                mock_db.execute.return_value = mock_result

                result = await analyze_stock_impl("AAPL")

                assert result["symbol"] == "AAPL"
                assert result["source"] == "research_pipeline"
                assert "recommendation" in result
                assert result["recommendation"]["action"] == "buy"
                assert result["recommendation"]["confidence"] == "high"
                assert len(result["recommendation"]["buy_zones"]) > 0
                assert result["quote"]["price"] == pytest.approx(105.0)


@pytest.mark.asyncio
async def test_analyze_stock_pipeline_compat_fallback(mock_ohlcv_df):
    """Verify fallback to legacy path on pipeline error."""

    with patch("app.mcp_server.tooling.analysis_analyze.settings") as mock_settings:
        mock_settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED = True
        mock_settings.RESEARCH_PIPELINE_ENABLED = True

        # Force an error in the pipeline path
        with patch(
            "app.analysis.pipeline.run_research_session",
            side_effect=Exception("Pipeline Crash"),
        ):
            # We need to mock the legacy path components to avoid real API calls
            with patch(
                "app.mcp_server.tooling.analysis_analyze._fetch_ohlcv_for_indicators",
                new_callable=AsyncMock,
            ) as mock_ohlcv:
                mock_ohlcv.return_value = mock_ohlcv_df

                with patch(
                    "app.mcp_server.tooling.analysis_analyze._get_quote_impl",
                    new_callable=AsyncMock,
                ) as mock_quote:
                    mock_quote.return_value = {
                        "price": 105.0,
                        "symbol": "AAPL",
                        "instrument_type": "equity_us",
                        "source": "yahoo",
                    }

                    with patch(
                        "app.mcp_server.tooling.analysis_analyze._get_indicators_impl",
                        new_callable=AsyncMock,
                    ) as mock_ind:
                        mock_ind.return_value = {"rsi": {"value": 50}}

                        with patch(
                            "app.mcp_server.tooling.analysis_analyze._get_support_resistance_impl",
                            new_callable=AsyncMock,
                        ) as mock_sr:
                            mock_sr.return_value = {}

                            # Execute
                            result = await analyze_stock_impl("AAPL")

                            # Verify result comes from legacy path
                            assert result["symbol"] == "AAPL"
                            assert result["source"] == "yahoo"  # Default for US stock
                            assert "quote" in result
                            assert "indicators" in result
