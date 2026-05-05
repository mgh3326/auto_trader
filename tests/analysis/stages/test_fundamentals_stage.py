from unittest.mock import AsyncMock, patch

import pytest

from app.analysis.stages.base import StageContext
from app.analysis.stages.fundamentals_stage import FundamentalsStageAnalyzer
from app.schemas.research_pipeline import FundamentalsSignals, StageVerdict


@pytest.mark.asyncio
async def test_fundamentals_stage_bull_verdict():
    # PER 10, Sector Median 15 -> 10 < 15 * 0.8 (12) -> BULL
    analyzer = FundamentalsStageAnalyzer()
    ctx = StageContext(
        session_id=1, symbol="005930", symbol_name="Samsung", instrument_type="equity_kr"
    )

    mock_data = {
        "per": 10.0,
        "pbr": 1.5,
        "market_cap": 500000000000,
        "sector": "Electronics",
        "peers": [
            {"per": 12.0},
            {"per": 15.0},
            {"per": 18.0},
            {"per": 20.0},
        ],
    }

    with patch(
        "app.analysis.stages.fundamentals_stage._fetch_fundamentals",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = mock_data
        output = await analyzer.analyze(ctx)

        assert output.stage_type == "fundamentals"
        assert output.verdict == StageVerdict.BULL
        assert isinstance(output.signals, FundamentalsSignals)
        assert output.signals.per == pytest.approx(10.0)
        assert output.signals.relative_per_vs_peers is not None
        # Median of [10, 12, 15, 18, 20] is 15.0
        # 10.0 / 15.0 = 0.666...
        assert output.signals.relative_per_vs_peers < 0.8


@pytest.mark.asyncio
async def test_fundamentals_stage_bear_verdict():
    # PER 30, Sector Median 15 -> 30 > 15 * 1.5 (22.5) -> BEAR
    analyzer = FundamentalsStageAnalyzer()
    ctx = StageContext(
        session_id=1, symbol="005930", symbol_name="Samsung", instrument_type="equity_kr"
    )

    mock_data = {
        "per": 30.0,
        "pbr": 1.5,
        "market_cap": 500000000000,
        "sector": "Electronics",
        "peers": [
            {"per": 12.0},
            {"per": 15.0},
            {"per": 18.0},
            {"per": 20.0},
        ],
    }

    with patch(
        "app.analysis.stages.fundamentals_stage._fetch_fundamentals",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = mock_data
        output = await analyzer.analyze(ctx)

        assert output.stage_type == "fundamentals"
        assert output.verdict == StageVerdict.BEAR


@pytest.mark.asyncio
async def test_fundamentals_stage_unavailable_for_crypto():
    analyzer = FundamentalsStageAnalyzer()
    ctx = StageContext(
        session_id=1, symbol="BTC", symbol_name="Bitcoin", instrument_type="crypto"
    )

    output = await analyzer.analyze(ctx)
    assert output.verdict == StageVerdict.UNAVAILABLE
