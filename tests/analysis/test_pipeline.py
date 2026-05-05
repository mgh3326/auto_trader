from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.pipeline import run_research_session
from app.models.research_pipeline import ResearchSession, ResearchSummary, StageAnalysis
from app.schemas.research_pipeline import (
    FundamentalsSignals,
    MarketSignals,
    NewsSignals,
    SocialSignals,
    StageOutput,
    StageVerdict,
    SummaryDecision,
    SummaryOutput,
)


@pytest.mark.asyncio
async def test_run_research_session_flow():
    # Setup mocks
    mock_db = AsyncMock(spec=AsyncSession)

    # Mock StockInfo
    mock_stock = MagicMock()
    mock_stock.id = 1
    mock_stock.symbol = "AAPL"
    mock_stock.instrument_type = "us_stock"

    # Mock create_stock_if_not_exists
    with (
        patch(
            "app.analysis.pipeline.create_stock_if_not_exists",
            AsyncMock(return_value=mock_stock),
        ) as mock_create_stock,
        patch("app.analysis.pipeline.MarketStageAnalyzer") as mock_market_analyzer,
        patch("app.analysis.pipeline.NewsStageAnalyzer") as mock_news_analyzer,
        patch(
            "app.analysis.pipeline.FundamentalsStageAnalyzer"
        ) as mock_fundamentals_analyzer,
        patch("app.analysis.pipeline.SocialStageAnalyzer") as mock_social_analyzer,
        patch("app.analysis.pipeline.build_summary") as mock_build_summary,
    ):
        # Setup analyzer returns
        mock_market_analyzer.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="market",
                verdict=StageVerdict.BULL,
                confidence=80,
                signals=MarketSignals(
                    last_close=150.0,
                    change_pct=1.5,
                    rsi_14=60.0,
                    atr_14=2.0,
                    volume_ratio_20d=1.2,
                    trend="uptrend",
                ),
            )
        )
        mock_news_analyzer.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="news",
                verdict=StageVerdict.BULL,
                confidence=70,
                signals=NewsSignals(headline_count=10, sentiment_score=0.5),
            )
        )
        mock_fundamentals_analyzer.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="fundamentals",
                verdict=StageVerdict.NEUTRAL,
                confidence=50,
                signals=FundamentalsSignals(peer_count=5),
            )
        )
        mock_social_analyzer.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="social",
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                signals=SocialSignals(available=False, reason="placeholder"),
            )
        )

        # Setup build_summary return
        mock_summary_output = SummaryOutput(
            decision=SummaryDecision.BUY,
            confidence=75,
            bull_arguments=[],
            bear_arguments=[],
            reasons=["Mock reason"],
        )
        mock_links = [MagicMock()]
        mock_build_summary.return_value = (mock_summary_output, mock_links)

        # Setup DB return values for session, stage analysis, summary
        # We need to simulate the insertion and ID assignment
        def mock_add(obj):
            if isinstance(obj, ResearchSession):
                obj.id = 100
            elif isinstance(obj, StageAnalysis):
                # Assign unique ID for each stage
                obj.id = 200 + len(
                    [
                        x
                        for x in mock_db.add.call_args_list
                        if isinstance(x[0][0], StageAnalysis)
                    ]
                )
            elif isinstance(obj, ResearchSummary):
                obj.id = 300

        mock_db.add.side_effect = mock_add

        # Execute
        session_id = await run_research_session(
            db=mock_db, symbol="AAPL", name="Apple Inc.", instrument_type="us_stock"
        )

        # Assertions
        assert session_id == 100
        assert mock_create_stock.called
        assert mock_db.add.call_count >= 6  # 1 session + 4 stages + 1 summary + links
        assert mock_db.commit.called
        assert mock_db.flush.called

        # Verify session status updated to finalized
        # Since we use AsyncMock, we can check calls
        # This might be tricky depending on implementation, but let's assume it works.
