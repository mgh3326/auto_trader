from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.analysis.stages.base import StageContext
from app.analysis.stages.news_stage import NewsStageAnalyzer
from app.schemas.research_pipeline import NewsSignals, StageVerdict


@pytest.mark.asyncio
async def test_news_stage_analyzer_bull_verdict():
    # Mock data
    mock_raw = {
        "headlines": [
            {"title": "Stock soaring on high demand", "sentiment": 0.8, "published_at": datetime.now(UTC) - timedelta(minutes=10)},
            {"title": "Positive earnings report", "sentiment": 0.6, "published_at": datetime.now(UTC) - timedelta(minutes=30)},
        ],
        "headline_count": 2,
        "sentiment_score": 0.7,
        "top_themes": ["Earnings", "Growth"],
        "urgent_flags": [],
        "newest_age_minutes": 10,
    }

    ctx = StageContext(session_id=1, symbol="AAPL", instrument_type="equity_us", user_id=1)
    analyzer = NewsStageAnalyzer()

    with patch("app.analysis.stages.news_stage._fetch_recent_headlines", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_raw

        output = await analyzer.analyze(ctx)

        assert output.stage_type == "news"
        assert output.verdict == StageVerdict.BULL
        assert isinstance(output.signals, NewsSignals)
        assert output.signals.headline_count == 2
        assert output.signals.sentiment_score == 0.7
        assert "Earnings" in output.signals.top_themes
        assert output.source_freshness.newest_age_minutes == 10


@pytest.mark.asyncio
async def test_news_stage_analyzer_unavailable():
    ctx = StageContext(session_id=1, symbol="UNKNOWN", instrument_type="equity_us", user_id=1)
    analyzer = NewsStageAnalyzer()

    with patch("app.analysis.stages.news_stage._fetch_recent_headlines", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = Exception("No news found")

        output = await analyzer.analyze(ctx)

        assert output.stage_type == "news"
        assert output.verdict == StageVerdict.UNAVAILABLE
        assert output.confidence == 0
