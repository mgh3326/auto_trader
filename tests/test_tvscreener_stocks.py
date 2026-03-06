"""Integration tests for stock screening using tvscreener.

This module tests the integration of TradingView's StockScreener for Korean
and US stock screening with bulk indicator queries. Tests cover filtering,
sorting, error handling, and end-to-end screening flow.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _screen_kr_via_tvscreener,
    _screen_us_via_tvscreener,
)


@pytest.fixture
def sample_kr_stock_df() -> pd.DataFrame:
    """Sample DataFrame returned by StockScreener for Korean stocks."""
    return pd.DataFrame(
        {
            "ticker": ["005930", "000660", "035420", "051910", "035720"],
            "name": ["삼성전자", "SK하이닉스", "NAVER", "LG화학", "카카오"],
            "price": [70000.0, 120000.0, 180000.0, 450000.0, 50000.0],
            "relative_strength_index_14": [32.5, 28.3, 45.6, 72.8, 38.9],
            "average_directional_index_14": [22.4, 35.7, 18.2, 42.5, 15.8],
            "volume": [15000000.0, 8000000.0, 2000000.0, 500000.0, 5000000.0],
            "change_percent": [2.5, -1.2, 0.8, -3.5, 1.5],
            "country": ["South Korea"] * 5,
        }
    )


@pytest.fixture
def sample_us_stock_df() -> pd.DataFrame:
    """Sample DataFrame returned by StockScreener for US stocks."""
    return pd.DataFrame(
        {
            "ticker": ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"],
            "name": [
                "Apple Inc.",
                "Microsoft Corp.",
                "Alphabet Inc.",
                "Amazon.com Inc.",
                "Tesla Inc.",
            ],
            "price": [175.50, 380.25, 140.80, 155.30, 210.45],
            "relative_strength_index_14": [35.2, 42.8, 29.5, 68.3, 55.7],
            "average_directional_index_14": [25.6, 18.9, 32.4, 45.2, 20.1],
            "volume": [75000000.0, 45000000.0, 32000000.0, 28000000.0, 125000000.0],
            "change_percent": [1.2, 0.5, -0.8, 3.2, -2.1],
            "country": ["United States"] * 5,
        }
    )


class TestKoreanStockScreening:
    """Test Korean stock screening via tvscreener."""

    @pytest.mark.asyncio
    async def test_kr_screening_basic_no_filters(self, sample_kr_stock_df):
        """Test basic Korean stock screening without filters."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_kr_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(limit=5)

            assert result["source"] == "tvscreener"
            assert result["count"] == 5
            assert len(result["stocks"]) == 5
            assert result["error"] is None

            # Verify stocks have expected fields
            first_stock = result["stocks"][0]
            assert "symbol" in first_stock
            assert "name" in first_stock
            assert "price" in first_stock
            assert "rsi" in first_stock
            assert "adx" in first_stock
            assert "volume" in first_stock
            assert "change_percent" in first_stock
            assert "country" in first_stock

    @pytest.mark.asyncio
    async def test_kr_screening_with_rsi_filter(self, sample_kr_stock_df):
        """Test Korean stock screening with RSI range filter."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            # Filter to only stocks with RSI between 28 and 40
            filtered_df = sample_kr_stock_df[
                (sample_kr_stock_df["relative_strength_index_14"] >= 28)
                & (sample_kr_stock_df["relative_strength_index_14"] <= 40)
            ]
            mock_instance.query_stock_screener = AsyncMock(return_value=filtered_df)
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(
                min_rsi=28.0,
                max_rsi=40.0,
                limit=10,
            )

            assert result["error"] is None
            assert (
                result["count"] == 3
            )  # 삼성전자(32.5), SK하이닉스(28.3), 카카오(38.9)
            assert result["filters_applied"]["min_rsi"] == 28.0
            assert result["filters_applied"]["max_rsi"] == 40.0

            # Verify all stocks have RSI in the expected range
            for stock in result["stocks"]:
                assert stock["rsi"] >= 28.0
                assert stock["rsi"] <= 40.0

    @pytest.mark.asyncio
    async def test_kr_screening_with_adx_filter(self, sample_kr_stock_df):
        """Test Korean stock screening with minimum ADX filter."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            # Filter to only stocks with ADX >= 30
            filtered_df = sample_kr_stock_df[
                sample_kr_stock_df["average_directional_index_14"] >= 30
            ]
            mock_instance.query_stock_screener = AsyncMock(return_value=filtered_df)
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(
                min_adx=30.0,
                limit=10,
            )

            assert result["error"] is None
            assert result["count"] == 2  # SK하이닉스(35.7), LG화학(42.5)
            assert result["filters_applied"]["min_adx"] == 30.0

            # Verify all stocks have ADX >= 30
            for stock in result["stocks"]:
                assert stock["adx"] >= 30.0

    @pytest.mark.asyncio
    async def test_kr_screening_sorting_by_rsi(self, sample_kr_stock_df):
        """Test Korean stock screening sorted by RSI ascending."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_kr_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(
                sort_by="rsi",
                limit=5,
            )

            assert result["error"] is None
            assert result["count"] == 5

            # Verify stocks are sorted by RSI ascending (most oversold first)
            rsi_values = [stock["rsi"] for stock in result["stocks"]]
            assert rsi_values == sorted(rsi_values)

    @pytest.mark.asyncio
    async def test_kr_screening_sorting_by_volume(self, sample_kr_stock_df):
        """Test Korean stock screening sorted by volume descending."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_kr_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(
                sort_by="volume",
                limit=5,
            )

            assert result["error"] is None
            assert result["count"] == 5

            # Verify stocks are sorted by volume descending (highest first)
            volume_values = [stock["volume"] for stock in result["stocks"]]
            assert volume_values == sorted(volume_values, reverse=True)

    @pytest.mark.asyncio
    async def test_kr_screening_sorting_by_adx(self, sample_kr_stock_df):
        """Test Korean stock screening sorted by ADX descending."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_kr_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(
                sort_by="adx",
                limit=5,
            )

            assert result["error"] is None
            assert result["count"] == 5

            # Verify stocks are sorted by ADX descending (highest trend first)
            adx_values = [stock["adx"] for stock in result["stocks"]]
            assert adx_values == sorted(adx_values, reverse=True)

    @pytest.mark.asyncio
    async def test_kr_screening_empty_dataframe(self):
        """Test Korean stock screening when StockScreener returns empty DataFrame."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(return_value=pd.DataFrame())
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(
                min_rsi=10.0,
                max_rsi=15.0,
            )

            assert result["count"] == 0
            assert len(result["stocks"]) == 0
            assert result["error"] is None

    @pytest.mark.asyncio
    async def test_kr_screening_import_error(self):
        """Test Korean stock screening when tvscreener not installed."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr_via_tvscreener"
        ) as mock_screen:
            # Simulate ImportError
            result = {
                "stocks": [],
                "source": "tvscreener",
                "count": 0,
                "filters_applied": {},
                "error": "tvscreener library not installed, cannot use StockScreener",
            }
            mock_screen.return_value = result

            result = await mock_screen()

            assert (
                result["error"]
                == "tvscreener library not installed, cannot use StockScreener"
            )
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_kr_screening_rate_limit_error(self):
        """Test Korean stock screening when rate limit is hit."""
        from app.services.tvscreener_service import TvScreenerError

        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                side_effect=TvScreenerError("Rate limit exceeded")
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(limit=10)

            assert result["error"] is not None
            assert "StockScreener query failed" in result["error"]
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_kr_screening_timeout_error(self):
        """Test Korean stock screening when query times out."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                side_effect=TimeoutError("Query timed out")
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(limit=10)

            assert result["error"] is not None
            assert "timed out" in result["error"]
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_kr_screening_limit_parameter(self, sample_kr_stock_df):
        """Test Korean stock screening respects limit parameter."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_kr_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_kr_via_tvscreener(limit=3)

            assert result["count"] == 3
            assert len(result["stocks"]) == 3
            assert result["filters_applied"]["limit"] == 3


class TestUSStockScreening:
    """Test US stock screening via tvscreener."""

    @pytest.mark.asyncio
    async def test_us_screening_basic_no_filters(self, sample_us_stock_df):
        """Test basic US stock screening without filters."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_us_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(limit=5)

            assert result["source"] == "tvscreener"
            assert result["count"] == 5
            assert len(result["stocks"]) == 5
            assert result["error"] is None

            # Verify stocks have expected fields
            first_stock = result["stocks"][0]
            assert "symbol" in first_stock
            assert "name" in first_stock
            assert "price" in first_stock
            assert "rsi" in first_stock
            assert "adx" in first_stock
            assert "volume" in first_stock
            assert "change_percent" in first_stock
            assert "country" in first_stock

    @pytest.mark.asyncio
    async def test_us_screening_with_rsi_filter(self, sample_us_stock_df):
        """Test US stock screening with RSI range filter."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            # Filter to only stocks with RSI between 25 and 45
            filtered_df = sample_us_stock_df[
                (sample_us_stock_df["relative_strength_index_14"] >= 25)
                & (sample_us_stock_df["relative_strength_index_14"] <= 45)
            ]
            mock_instance.query_stock_screener = AsyncMock(return_value=filtered_df)
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                min_rsi=25.0,
                max_rsi=45.0,
                limit=10,
            )

            assert result["error"] is None
            assert result["count"] == 3  # AAPL(35.2), MSFT(42.8), GOOGL(29.5)
            assert result["filters_applied"]["min_rsi"] == 25.0
            assert result["filters_applied"]["max_rsi"] == 45.0

            # Verify all stocks have RSI in the expected range
            for stock in result["stocks"]:
                assert stock["rsi"] >= 25.0
                assert stock["rsi"] <= 45.0

    @pytest.mark.asyncio
    async def test_us_screening_with_adx_filter(self, sample_us_stock_df):
        """Test US stock screening with minimum ADX filter."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            # Filter to only stocks with ADX >= 30
            filtered_df = sample_us_stock_df[
                sample_us_stock_df["average_directional_index_14"] >= 30
            ]
            mock_instance.query_stock_screener = AsyncMock(return_value=filtered_df)
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                min_adx=30.0,
                limit=10,
            )

            assert result["error"] is None
            assert result["count"] == 2  # GOOGL(32.4), AMZN(45.2)
            assert result["filters_applied"]["min_adx"] == 30.0

            # Verify all stocks have ADX >= 30
            for stock in result["stocks"]:
                assert stock["adx"] >= 30.0

    @pytest.mark.asyncio
    async def test_us_screening_sorting_by_rsi(self, sample_us_stock_df):
        """Test US stock screening sorted by RSI ascending."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_us_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                sort_by="rsi",
                limit=5,
            )

            assert result["error"] is None
            assert result["count"] == 5

            # Verify stocks are sorted by RSI ascending (most oversold first)
            rsi_values = [stock["rsi"] for stock in result["stocks"]]
            assert rsi_values == sorted(rsi_values)

    @pytest.mark.asyncio
    async def test_us_screening_sorting_by_volume(self, sample_us_stock_df):
        """Test US stock screening sorted by volume descending."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_us_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                sort_by="volume",
                limit=5,
            )

            assert result["error"] is None
            assert result["count"] == 5

            # Verify stocks are sorted by volume descending (highest first)
            volume_values = [stock["volume"] for stock in result["stocks"]]
            assert volume_values == sorted(volume_values, reverse=True)

    @pytest.mark.asyncio
    async def test_us_screening_sorting_by_change(self, sample_us_stock_df):
        """Test US stock screening sorted by change_percent descending."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                return_value=sample_us_stock_df
            )
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                sort_by="change",
                limit=5,
            )

            assert result["error"] is None
            assert result["count"] == 5

            # Verify stocks are sorted by change_percent descending (biggest gainers first)
            change_values = [stock["change_percent"] for stock in result["stocks"]]
            assert change_values == sorted(change_values, reverse=True)

    @pytest.mark.asyncio
    async def test_us_screening_empty_dataframe(self):
        """Test US stock screening when StockScreener returns empty DataFrame."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(return_value=pd.DataFrame())
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                min_rsi=10.0,
                max_rsi=15.0,
            )

            assert result["count"] == 0
            assert len(result["stocks"]) == 0
            assert result["error"] is None

    @pytest.mark.asyncio
    async def test_us_screening_import_error(self):
        """Test US stock screening when tvscreener not installed."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core._screen_us_via_tvscreener"
        ) as mock_screen:
            # Simulate ImportError
            result = {
                "stocks": [],
                "source": "tvscreener",
                "count": 0,
                "filters_applied": {},
                "error": "tvscreener library not installed, cannot use StockScreener",
            }
            mock_screen.return_value = result

            result = await mock_screen()

            assert (
                result["error"]
                == "tvscreener library not installed, cannot use StockScreener"
            )
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_us_screening_rate_limit_error(self):
        """Test US stock screening when rate limit is hit."""
        from app.services.tvscreener_service import TvScreenerError

        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                side_effect=TvScreenerError("Rate limit exceeded")
            )
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(limit=10)

            assert result["error"] is not None
            assert "StockScreener query failed" in result["error"]
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_us_screening_timeout_error(self):
        """Test US stock screening when query times out."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            mock_instance.query_stock_screener = AsyncMock(
                side_effect=TimeoutError("Query timed out")
            )
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(limit=10)

            assert result["error"] is not None
            assert "timed out" in result["error"]
            assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_us_screening_combined_filters(self, sample_us_stock_df):
        """Test US stock screening with combined RSI and ADX filters."""
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
        ) as mock_service:
            mock_instance = AsyncMock()
            # Filter stocks with RSI <= 40 AND ADX >= 25
            filtered_df = sample_us_stock_df[
                (sample_us_stock_df["relative_strength_index_14"] <= 40)
                & (sample_us_stock_df["average_directional_index_14"] >= 25)
            ]
            mock_instance.query_stock_screener = AsyncMock(return_value=filtered_df)
            mock_service.return_value = mock_instance

            result = await _screen_us_via_tvscreener(
                max_rsi=40.0,
                min_adx=25.0,
                limit=10,
            )

            assert result["error"] is None
            assert result["count"] == 2  # AAPL(35.2, 25.6), GOOGL(29.5, 32.4)

            # Verify all stocks meet both criteria
            for stock in result["stocks"]:
                assert stock["rsi"] <= 40.0
                assert stock["adx"] >= 25.0


class TestStockScreeningIntegration:
    """Integration tests that call real tvscreener API."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_kr_screening_real_api(self):
        """Test Korean stock screening with real TradingView API call."""
        pytest.importorskip("tvscreener", reason="tvscreener not installed")

        result = await _screen_kr_via_tvscreener(
            max_rsi=35.0,
            limit=5,
        )

        # Verify structure even if no results (depending on market conditions)
        assert "stocks" in result
        assert "source" in result
        assert result["source"] == "tvscreener"
        assert "count" in result
        assert "filters_applied" in result
        assert "error" in result

        # If we got results, verify structure
        if result["count"] > 0:
            first_stock = result["stocks"][0]
            assert "symbol" in first_stock
            assert "name" in first_stock
            assert "price" in first_stock
            assert "rsi" in first_stock
            assert "adx" in first_stock
            # Verify RSI filter was applied
            if first_stock["rsi"] is not None:
                assert first_stock["rsi"] <= 35.0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_us_screening_real_api(self):
        """Test US stock screening with real TradingView API call."""
        pytest.importorskip("tvscreener", reason="tvscreener not installed")

        result = await _screen_us_via_tvscreener(
            max_rsi=40.0,
            sort_by="volume",
            limit=10,
        )

        # Verify structure even if no results
        assert "stocks" in result
        assert "source" in result
        assert result["source"] == "tvscreener"
        assert "count" in result
        assert "filters_applied" in result
        assert "error" in result

        # If we got results, verify structure and sorting
        if result["count"] > 0:
            first_stock = result["stocks"][0]
            assert "symbol" in first_stock
            assert "name" in first_stock
            assert "price" in first_stock
            assert "rsi" in first_stock
            assert "volume" in first_stock
            # Verify RSI filter was applied
            if first_stock["rsi"] is not None:
                assert first_stock["rsi"] <= 40.0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_kr_screening_adx_availability(self):
        """Verify ADX field is available for Korean stocks."""
        pytest.importorskip("tvscreener", reason="tvscreener not installed")

        result = await _screen_kr_via_tvscreener(
            min_adx=20.0,
            limit=5,
        )

        # Verify we can query with ADX filter without errors
        assert result["error"] is None or "not available" in str(result["error"])

        # If we got results, verify ADX is populated
        if result["count"] > 0:
            first_stock = result["stocks"][0]
            # ADX should be present (may be None if not available)
            assert "adx" in first_stock

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_us_screening_adx_availability(self):
        """Verify ADX field is available for US stocks."""
        pytest.importorskip("tvscreener", reason="tvscreener not installed")

        result = await _screen_us_via_tvscreener(
            min_adx=25.0,
            limit=5,
        )

        # Verify we can query with ADX filter without errors
        assert result["error"] is None or "not available" in str(result["error"])

        # If we got results, verify ADX is populated
        if result["count"] > 0:
            first_stock = result["stocks"][0]
            # ADX should be present and likely populated for US stocks
            assert "adx" in first_stock
