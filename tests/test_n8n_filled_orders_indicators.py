from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest


def _make_ohlcv_df(n: int = 250, base_close: float = 100_000_000) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with enough rows for all indicators."""
    # Use sine wave + trend to create both gains and losses for RSI calculation
    import numpy as np

    t = np.linspace(0, 4 * np.pi, n)
    noise = np.random.RandomState(42).normal(0, 0.02, n)
    close_values = base_close * (1 + 0.1 * np.sin(t) + 0.05 * np.linspace(0, 1, n) + noise)
    close = pd.Series(close_values, dtype=float)

    return pd.DataFrame(
        {
            "open": close - 5_000,
            "high": close + 10_000,
            "low": close - 10_000,
            "close": close,
            "volume": [1_000_000.0 + i * 100 for i in range(n)],
        }
    )


@pytest.mark.unit
class TestComputeReviewIndicators:
    @pytest.mark.asyncio
    async def test_returns_all_indicator_fields_for_crypto(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(250)

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is not None
        # All N8nTradeReviewIndicators fields except fear_greed (fetched separately)
        for key in (
            "rsi_14",
            "rsi_7",
            "ema_20",
            "ema_200",
            "macd",
            "macd_signal",
            "adx",
            "stoch_rsi_k",
            "volume_ratio",
        ):
            assert key in result, f"Missing key: {key}"

        assert isinstance(result["rsi_14"], float)
        assert isinstance(result["ema_20"], float)
        assert isinstance(result["ema_200"], float)
        assert isinstance(result["macd"], float)
        assert isinstance(result["volume_ratio"], float)

    @pytest.mark.asyncio
    async def test_returns_none_on_insufficient_data(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(10)  # Too few rows

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_fetch_error(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is None

    @pytest.mark.asyncio
    async def test_works_for_equity_kr(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(250, base_close=80_000)

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("005930", "equity_kr")

        assert result is not None
        assert isinstance(result["rsi_14"], float)
        assert isinstance(result["ema_20"], float)

    @pytest.mark.asyncio
    async def test_volume_ratio_with_zero_avg_volume(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(250)
        df["volume"] = 0.0  # All zeros

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is not None
        assert result["volume_ratio"] is None
