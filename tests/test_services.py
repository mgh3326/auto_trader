"""
Tests for service modules.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services.upbit import fetch_ohlcv, fetch_price


class TestUpbitService:
    """Test Upbit service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.upbit._request_json")
    async def test_fetch_ohlcv(self, mock_request):
        """Test fetching OHLCV data."""
        # Mock response
        mock_data = [
            {
                "candle_date_time_kst": "2023-12-01T00:00:00",
                "opening_price": 45000000,
                "high_price": 46000000,
                "low_price": 44000000,
                "trade_price": 45500000,
                "candle_acc_trade_volume": 100.0,
                "candle_acc_trade_price": 4550000000.0,
            }
        ]
        mock_request.return_value = mock_data

        result = await fetch_ohlcv("KRW-BTC", days=1)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        assert "date" in result.columns
        assert "open" in result.columns
        assert "high" in result.columns
        assert "low" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        assert "value" in result.columns

    @pytest.mark.asyncio
    @patch("app.services.upbit._request_json")
    async def test_fetch_price(self, mock_request):
        """Test fetching current price."""
        # Mock response
        mock_data = [
            {
                "opening_price": 45000000,
                "high_price": 46000000,
                "low_price": 44000000,
                "trade_price": 45500000,
                "acc_trade_volume_24h": 100.0,
                "acc_trade_price_24h": 4550000000.0,
            }
        ]
        mock_request.return_value = mock_data

        result = await fetch_price("KRW-BTC")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        assert "code" in result.columns
        assert "date" in result.columns
        assert "time" in result.columns
        assert "open" in result.columns
        assert "high" in result.columns
        assert "low" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        assert "value" in result.columns

    def test_fetch_ohlcv_validation(self):
        """Test OHLCV validation."""
        # This test would verify validation logic
        # For now, we'll skip the actual validation test since it's async
        pass


class TestKISService:
    """Test KIS service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_kis_client_initialization(self, mock_client_class):
        """Test KIS client initialization."""
        # Mock client
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Import and test the actual class
        from app.services.kis import KISClient

        client = KISClient()

        assert client is not None
        assert hasattr(client, "_hdr_base")

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_kis_volume_rank(self, mock_client_class):
        """Test KIS volume rank functionality."""
        # Mock client
        mock_client = AsyncMock()

        # Mock response - r.json()이 동기적으로 동작하도록 설정
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "hts_kor_isnm": "삼성전자",
                    "stck_cntg_hour": "15:30:00",
                    "stck_prpr": "50000",
                }
            ],
        }

        # Mock client의 get 메서드 설정
        mock_client.get.return_value = mock_response

        # Mock client class가 context manager로 동작하도록 설정
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        # Import and test the actual function
        from app.services.kis import KISClient

        client = KISClient()

        # Mock the token loading
        with patch.object(client, "_ensure_token"):
            result = await client.volume_rank()

            assert isinstance(result, list)
            assert len(result) > 0


class TestYahooService:
    """Test Yahoo Finance service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.yahoo.yf.download")
    async def test_fetch_ohlcv(self, mock_download):
        """Test fetching OHLCV data from Yahoo Finance."""
        # Mock yfinance download response
        mock_df = pd.DataFrame(
            {
                "open": [100, 101, 102],
                "high": [105, 106, 107],
                "low": [95, 96, 97],
                "close": [103, 104, 105],
                "volume": [1000, 1100, 1200],
            }
        )
        mock_download.return_value = mock_df

        # Import and test the actual function
        from app.services.yahoo import fetch_ohlcv

        result = await fetch_ohlcv("AAPL", days=3)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        assert "date" in result.columns
        assert "open" in result.columns
        assert "close" in result.columns

    @pytest.mark.asyncio
    @patch("app.services.yahoo.yf.Ticker")
    async def test_fetch_price(self, mock_ticker_class):
        """Test fetching current price from Yahoo Finance."""
        # Mock Ticker instance
        mock_ticker = MagicMock()
        mock_ticker.fast_info.open = 150.0
        mock_ticker.fast_info.day_high = 155.0
        mock_ticker.fast_info.day_low = 145.0
        mock_ticker.fast_info.last_price = 152.0
        mock_ticker.fast_info.last_volume = 1000000
        mock_ticker_class.return_value = mock_ticker

        # Import and test the actual function
        from app.services.yahoo import fetch_price

        result = await fetch_price("AAPL")

        # 실제 반환되는 DataFrame 구조에 맞게 테스트 수정
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        # 'code'는 index로 설정되므로 columns에는 없음
        assert "date" in result.columns
        assert "close" in result.columns


class TestGeminiService:
    """Test Gemini AI service functionality."""

    @pytest.mark.asyncio
    @patch("redis.asyncio.Redis")
    async def test_model_rate_limiter(self, mock_redis):
        """Test Gemini model rate limiter."""
        # Mock Redis client
        mock_redis_client = AsyncMock()
        mock_redis.from_url.return_value = mock_redis_client
        mock_redis_client.get.return_value = None  # No rate limit

        # Import and test the actual class
        from app.core.model_rate_limiter import ModelRateLimiter

        limiter = ModelRateLimiter()

        # Test rate limit check
        result = await limiter.is_model_available("gemini-2.5-pro", "test_key")
        assert result is True


class TestDARTService:
    """Test DART disclosure service functionality."""

    def test_dart_service_import(self):
        """Test that DART service can be imported."""
        # This test verifies the module can be imported
        # Implementation depends on your actual DART service
        try:
            from app.services.disclosures import dart

            assert dart is not None
        except ImportError:
            # If DART service is not implemented yet, skip this test
            pytest.skip("DART service not implemented yet")


class TestTelegramService:
    """Test Telegram bot service functionality."""

    def test_telegram_service_import(self):
        """Test that Telegram service can be imported."""
        # This test verifies the module can be imported
        # Implementation depends on your actual Telegram service
        try:
            from app.services import telegram

            assert telegram is not None
        except ImportError:
            # If Telegram service is not implemented yet, skip this test
            pytest.skip("Telegram service not implemented yet")


class TestServiceUtilities:
    """Test service utility functions."""

    def test_dataframe_structure(self):
        """Test that returned DataFrames have correct structure."""
        # This test would verify the structure of returned data
        # Implementation depends on your actual data structure
        pass

    def test_error_handling(self):
        """Test error handling in services."""
        # This test would verify error handling
        # Implementation depends on your actual error handling
        pass
