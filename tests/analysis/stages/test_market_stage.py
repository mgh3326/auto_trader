from unittest.mock import AsyncMock

import pytest

from app.analysis.stages.base import StageContext
from app.analysis.stages.market_stage import MarketStageAnalyzer
from app.schemas.research_pipeline import MarketSignals, StageVerdict


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_stage_basic_signals(monkeypatch):
    # Patch the *single* data source the stage uses.
    fake_ohlcv = AsyncMock(
        return_value={
            "last_close": 100.0,
            "change_pct": 1.5,
            "rsi_14": 60.0,
            "atr_14": 1.2,
            "volume_ratio_20d": 1.5,
            "trend": "uptrend",
            "snapshot_at_iso": "2026-05-05T08:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        "app.analysis.stages.market_stage._fetch_market_snapshot",
        fake_ohlcv,
    )

    stage = MarketStageAnalyzer()
    out = await stage.run(
        StageContext(session_id=1, symbol="005930", instrument_type="equity_kr")
    )
    assert isinstance(out.signals, MarketSignals)
    assert out.signals.last_close == pytest.approx(100.0)
    assert out.verdict in {StageVerdict.BULL, StageVerdict.NEUTRAL, StageVerdict.BEAR}
    assert out.source_freshness is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_market_snapshot(monkeypatch):
    import numpy as np
    import pandas as pd

    # Mock OHLCV data: 70 days of increasing prices
    dates = pd.date_range(end="2026-05-05", periods=70)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.linspace(50, 119, 70),
            "high": np.linspace(55, 125, 70),
            "low": np.linspace(45, 115, 70),
            "close": np.linspace(50, 120, 70),
            "volume": [1000] * 70,
        }
    )

    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(
        "app.analysis.stages.market_stage._fetch_ohlcv_for_indicators", mock_fetch
    )

    from app.analysis.stages.market_stage import _fetch_market_snapshot

    res = await _fetch_market_snapshot("005930", "equity_kr")

    assert res["last_close"] == pytest.approx(120.0)
    assert res["change_pct"] > 0
    assert "rsi_14" in res
    assert "atr_14" in res
    assert res["volume_ratio_20d"] == pytest.approx(1.0)  # (1000 / 1000)
    assert res["trend"] == "uptrend"
