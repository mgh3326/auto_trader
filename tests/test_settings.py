"""
Test environment settings and configuration.
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def test_env():
    """Set up test environment variables."""
    test_env_vars = {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "API_KEY": "test_key",
        "SECRET_KEY": "test_secret_key",
        "DEBUG": "true",
    }

    with patch.dict(os.environ, test_env_vars):
        yield test_env_vars


@pytest.fixture
def mock_external_services():
    """Mock external service calls for testing."""
    with (
        patch("app.services.upbit.httpx.AsyncClient") as mock_upbit,
        patch("app.services.yahoo.yfinance.Ticker") as mock_yahoo,
        patch("app.services.kis.httpx.AsyncClient") as mock_kis,
    ):
        yield {"upbit": mock_upbit, "yahoo": mock_yahoo, "kis": mock_kis}


@pytest.fixture
def sample_test_data():
    """Provide sample test data for various test scenarios."""
    return {
        "stocks": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "price": 150.0,
                "change": 2.5,
                "change_percent": 1.69,
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft Corporation",
                "price": 300.0,
                "change": -1.5,
                "change_percent": -0.50,
            },
        ],
        "cryptos": [
            {
                "symbol": "BTC",
                "name": "Bitcoin",
                "price": 45000.0,
                "change": 500.0,
                "change_percent": 1.12,
            },
            {
                "symbol": "ETH",
                "name": "Ethereum",
                "price": 3000.0,
                "change": -50.0,
                "change_percent": -1.64,
            },
        ],
        "analysis_results": [
            {
                "symbol": "AAPL",
                "analysis_type": "technical",
                "result": "BUY",
                "confidence": 0.85,
                "indicators": {
                    "rsi": 30.5,
                    "macd": "bullish",
                    "moving_averages": "above",
                },
            },
            {
                "symbol": "MSFT",
                "analysis_type": "technical",
                "result": "HOLD",
                "confidence": 0.60,
                "indicators": {
                    "rsi": 55.0,
                    "macd": "neutral",
                    "moving_averages": "neutral",
                },
            },
        ],
    }
