"""
Pytest configuration and common fixtures for auto-trader tests.
"""
import asyncio
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.core.config import settings


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def app_settings():
    """Get application settings."""
    return settings


@pytest.fixture
def mock_db():
    """Mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """Mock HTTP client."""
    return AsyncMock()


@pytest.fixture
def mock_external_services():
    """Mock all external service calls for testing."""
    with patch("app.services.upbit.httpx.AsyncClient") as mock_upbit, patch(
        "app.services.yahoo.yf.download"
    ) as mock_yahoo_download, patch(
        "app.services.yahoo.yf.Ticker"
    ) as mock_yahoo_ticker, patch(
        "app.services.kis.httpx.AsyncClient"
    ) as mock_kis, patch(
        "app.core.model_rate_limiter.redis.asyncio.Redis"
    ) as mock_redis:
        # Configure mock responses
        yield {
            "upbit": mock_upbit,
            "yahoo_download": mock_yahoo_download,
            "yahoo_ticker": mock_yahoo_ticker,
            "kis": mock_kis,
            "redis": mock_redis,
        }


@pytest.fixture
def mock_kis_service():
    """Mock KIS service responses."""
    mock_kis = AsyncMock()

    # Mock access token response
    mock_kis.post.return_value = AsyncMock(
        status_code=200,
        json=AsyncMock(
            return_value={"access_token": "test_kis_token", "expires_in": 3600}
        ),
    )

    # Mock stock price response
    mock_kis.get.return_value = AsyncMock(
        status_code=200,
        json=AsyncMock(
            return_value={
                "rt_cd": "0",
                "output": {"stck_prpr": 50000, "prdy_vrss": 1000, "prdy_ctrt": 2.0},
            }
        ),
    )

    return mock_kis


@pytest.fixture
def mock_yahoo_service():
    """Mock Yahoo Finance service responses."""
    # Mock yfinance download
    mock_download = MagicMock()
    mock_download.return_value = pd.DataFrame(
        {
            "open": [100, 101, 102],
            "high": [105, 106, 107],
            "low": [95, 96, 97],
            "close": [103, 104, 105],
            "volume": [1000, 1100, 1200],
        }
    )

    # Mock Ticker instance
    mock_ticker = MagicMock()
    mock_ticker.fast_info.open = 150.0
    mock_ticker.fast_info.day_high = 155.0
    mock_ticker.fast_info.day_low = 145.0
    mock_ticker.fast_info.last_price = 152.0
    mock_ticker.fast_info.last_volume = 1000000

    return {"download": mock_download, "ticker": mock_ticker}


@pytest.fixture
def mock_redis_service():
    """Mock Redis service responses."""
    mock_redis = AsyncMock()

    # Mock Redis client
    mock_redis_client = AsyncMock()
    mock_redis.from_url.return_value = mock_redis_client
    mock_redis_client.get.return_value = None  # No rate limit
    mock_redis_client.set.return_value = True

    return mock_redis


@pytest.fixture
def sample_stock_data():
    """Sample stock data for testing."""
    return {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 150.0,
        "change": 2.5,
        "change_percent": 1.69,
    }


@pytest.fixture
def sample_crypto_data():
    """Sample cryptocurrency data for testing."""
    return {
        "symbol": "BTC",
        "name": "Bitcoin",
        "price": 45000.0,
        "change": 500.0,
        "change_percent": 1.12,
    }


@pytest.fixture
def sample_analysis_result():
    """Sample analysis result for testing."""
    return {
        "symbol": "AAPL",
        "analysis_type": "technical",
        "result": "BUY",
        "confidence": 0.85,
        "indicators": {"rsi": 30.5, "macd": "bullish", "moving_averages": "above"},
    }


@pytest.fixture
def sample_kis_data():
    """Sample KIS API response data."""
    return {
        "access_token": "test_token_12345",
        "expires_in": 3600,
        "stock_price": {"stck_prpr": 50000, "prdy_vrss": 1000, "prdy_ctrt": 2.0},
    }


@pytest.fixture
def sample_yahoo_data():
    """Sample Yahoo Finance API response data."""
    return {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 150.0,
        "change": 2.5,
        "change_percent": 1.69,
        "volume": 1000000,
        "market_cap": 2500000000000,
    }


@pytest.fixture
def sample_gemini_response():
    """Sample Gemini AI response data."""
    return {
        "text": "Based on technical analysis, this stock shows bullish signals with RSI at 30.5 and MACD crossing above signal line.",
        "confidence": 0.85,
        "recommendation": "BUY",
    }


# Markers for different test types
pytest_plugins = ["pytest_asyncio"]


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")
