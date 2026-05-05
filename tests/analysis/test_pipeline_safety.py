from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.pipeline import run_research_session
from app.models.research_pipeline import ResearchSession, StageAnalysis
from app.schemas.research_pipeline import (
    FundamentalsSignals,
    MarketSignals,
    NewsSignals,
    StageOutput,
    StageVerdict,
    SummaryDecision,
    SummaryOutput,
)


@pytest.mark.asyncio
async def test_stage_failure_graceful_degradation():
    """
    Test Case A: A stage analyzer raises an Exception.
    Verify that the other stages still run and are saved, and a summary is still attempted.
    """
    mock_db = AsyncMock(spec=AsyncSession)

    # Mock StockInfo
    mock_stock = MagicMock()
    mock_stock.id = 1

    with (
        patch(
            "app.analysis.pipeline.create_stock_if_not_exists",
            AsyncMock(return_value=mock_stock),
        ),
        patch("app.analysis.pipeline.MarketStageAnalyzer") as mock_market_analyzer,
        patch("app.analysis.pipeline.NewsStageAnalyzer") as mock_news_analyzer,
        patch(
            "app.analysis.pipeline.FundamentalsStageAnalyzer"
        ) as mock_fundamentals_analyzer,
        patch("app.analysis.pipeline.build_summary") as mock_build_summary,
    ):
        # Market fails
        mock_market_analyzer.return_value.run = AsyncMock(
            side_effect=Exception("Market data timeout")
        )

        # Others succeed
        mock_news_analyzer.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="news",
                verdict=StageVerdict.BULL,
                confidence=70,
                signals=NewsSignals(headline_count=5, sentiment_score=0.5),
            )
        )
        mock_fundamentals_analyzer.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="fundamentals",
                verdict=StageVerdict.NEUTRAL,
                confidence=50,
                signals=FundamentalsSignals(peer_count=2),
            )
        )

        mock_summary_output = SummaryOutput(
            decision=SummaryDecision.HOLD,
            confidence=50,
            bull_arguments=[],
            bear_arguments=[],
            reasons=["Partial data"],
        )
        mock_build_summary.return_value = (mock_summary_output, [])

        # Execute
        await run_research_session(
            db=mock_db, symbol="AAPL", name="Apple", instrument_type="us_stock"
        )

        # Verify build_summary was called despite market failure
        assert mock_build_summary.called

        # Verify 2 StageAnalysis rows were added (excluding Market)
        # Social is removed, so only News and Fundamentals remain.
        stage_adds = [
            c for c in mock_db.add.call_args_list if isinstance(c[0][0], StageAnalysis)
        ]
        assert len(stage_adds) == 2
        added_stages = {c[0][0].stage_type for c in stage_adds}
        assert "market" not in added_stages
        assert "news" in added_stages


@pytest.mark.asyncio
async def test_commit_failure_safety():
    """
    Test Case B: The DB raises an error during the final commit (after summary generation).
    Verify that ResearchSession.status remains 'open' (not 'finalized').
    """
    mock_db = AsyncMock(spec=AsyncSession)

    # Mock StockInfo
    mock_stock = MagicMock()
    mock_stock.id = 1

    session_obj = None

    def mock_add(obj):
        nonlocal session_obj
        if isinstance(obj, ResearchSession):
            session_obj = obj
            obj.id = 123

    mock_db.add.side_effect = mock_add

    with (
        patch(
            "app.analysis.pipeline.create_stock_if_not_exists",
            AsyncMock(return_value=mock_stock),
        ),
        patch("app.analysis.pipeline.MarketStageAnalyzer") as mock_m,
        patch("app.analysis.pipeline.NewsStageAnalyzer") as mock_n,
        patch("app.analysis.pipeline.FundamentalsStageAnalyzer") as mock_f,
        patch("app.analysis.pipeline.build_summary") as mock_build_summary,
    ):
        mock_m.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="market",
                verdict=StageVerdict.BULL,
                confidence=80,
                signals=MarketSignals(
                    last_close=150.0,
                    change_pct=1.0,
                    rsi_14=50.0,
                    atr_14=1.0,
                    volume_ratio_20d=1.0,
                    trend="uptrend",
                ),
            )
        )
        mock_n.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="news",
                verdict=StageVerdict.BULL,
                confidence=70,
                signals=NewsSignals(headline_count=5, sentiment_score=0.5),
            )
        )
        mock_f.return_value.run = AsyncMock(
            return_value=StageOutput(
                stage_type="fundamentals",
                verdict=StageVerdict.NEUTRAL,
                confidence=50,
                signals=FundamentalsSignals(peer_count=2),
            )
        )

        mock_summary_output = SummaryOutput(
            decision=SummaryDecision.HOLD,
            confidence=50,
            bull_arguments=[],
            bear_arguments=[],
            reasons=[],
            model_name="test-model",
            prompt_version="1.0",
        )
        mock_build_summary.return_value = (mock_summary_output, [])

        # Mock commit to fail on the final call (status update)
        # We need to know which commit call it is.
        # In the refined implementation, we expect:
        # 1. commit per stage (3 stages -> 3 commits)
        # 2. commit for summary + links (1 commit)
        # 3. commit for status (1 commit)

        commit_count = 0

        async def mock_commit():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 5:  # The 5th commit is the final status update
                raise Exception("DB Connection Lost")

        mock_db.commit.side_effect = mock_commit

        with pytest.raises(Exception, match="DB Connection Lost"):
            await run_research_session(
                db=mock_db, symbol="AAPL", name="Apple", instrument_type="us_stock"
            )

        # Check session_obj status.
        # If it failed at the final commit, it should still be 'open' if we set it as the last step.
        assert session_obj.status == "open"
