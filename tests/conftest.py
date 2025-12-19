"""
Pytest configuration and common fixtures for auto-trader tests.
"""

import asyncio
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


def _load_env_file(env_path: Path) -> None:
    """Load environment variables from a simple KEY=VALUE file."""
    if not env_path.is_file():
        return

    with env_path.open(encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            os.environ.setdefault(key, value)


def _ensure_test_env() -> None:
    """Ensure required environment variables exist for tests."""
    project_root = Path(__file__).resolve().parents[1]
    env_example_path = project_root / "env.example"
    env_test_path = project_root / ".env.test"

    # 1) 기본값: env.example에 정의된 항목을 그대로 불러온다.
    _load_env_file(env_example_path)

    # Allow developers to provide a .env.test with custom overrides.
    if env_test_path.exists():
        _load_env_file(env_test_path)

    default_env_values = {
        "KIS_APP_KEY": "DUMMY_KIS_APP_KEY",
        "KIS_APP_SECRET": "DUMMY_KIS_APP_SECRET",
        "KIS_ACCESS_TOKEN": "",
        "KIS_ACCOUNT_NO": "00000000-00",
        "TELEGRAM_TOKEN": "DUMMY_TELEGRAM_TOKEN",
        "TELEGRAM_CHAT_IDS": "123456789,987654321",
        "TELEGRAM_CHAT_IDS_STR": "123456789,987654321",
        "GOOGLE_API_KEY": "DUMMY_GOOGLE_API_KEY",
        "GOOGLE_API_KEYS": "DUMMY_GOOGLE_API_KEY_1,DUMMY_GOOGLE_API_KEY_2",
        "OPENDART_API_KEY": "DUMMY_OPENDART_API_KEY",
        "UPBIT_ACCESS_KEY": "DUMMY_UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY": "DUMMY_UPBIT_SECRET_KEY",
        "UPBIT_BUY_AMOUNT": "100000",
        "UPBIT_MIN_KRW_BALANCE": "100000",
        "TOP_N": "30",
        "DROP_PCT": "-3.0",
        "CRON": "0 * * * *",
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db",
        "REDIS_URL": "redis://localhost:6379/0",
        "REDIS_MAX_CONNECTIONS": "10",
        "REDIS_SOCKET_TIMEOUT": "5",
        "REDIS_SOCKET_CONNECT_TIMEOUT": "5",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "localhost:4317",
        "OTEL_ENABLED": "false",
        "OTEL_INSECURE": "true",
        "OTEL_SERVICE_NAME": "auto-trader-test",
        "OTEL_SERVICE_VERSION": "0.1.0-test",
        "OTEL_ENVIRONMENT": "test",
        "ERROR_REPORTING_ENABLED": "false",
        "ERROR_REPORTING_CHAT_ID": "123456789",
        "ERROR_DUPLICATE_WINDOW": "300",
        "EXPOSE_MONITORING_TEST_ROUTES": "false",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "Test_Secret_Key_12345_Test_Secret_Key_12345",  # Valid complex key for tests
    }

    for key, value in default_env_values.items():
        os.environ.setdefault(key, value)

    # Force overwrite SECRET_KEY to ensure it passes validation during tests
    # regardless of what's in env.example or .env
    os.environ["SECRET_KEY"] = "Test_Secret_Key_12345_Test_Secret_Key_12345"


_ensure_test_env()

from app.core.config import settings


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop]:
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
    with (
        patch("app.services.upbit.httpx.AsyncClient") as mock_upbit,
        patch("app.services.yahoo.yf.download") as mock_yahoo_download,
        patch("app.services.yahoo.yf.Ticker") as mock_yahoo_ticker,
        patch("app.services.kis.httpx.AsyncClient") as mock_kis,
        patch("app.core.model_rate_limiter.redis.asyncio.Redis") as mock_redis,
    ):
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


@pytest.fixture(autouse=True)
def mock_auth_middleware_db():
    """Mock AsyncSessionLocal in AuthMiddleware to prevent DB connection attempts."""
    with patch("app.middleware.auth.AsyncSessionLocal") as mock:
        mock_session = AsyncMock()
        mock.return_value.__aenter__.return_value = mock_session
        yield mock_session


@pytest.fixture(scope="module")
def auth_mock_session():
    """Shared mock database session for auth tests."""
    return AsyncMock()


@pytest.fixture
def auth_test_client(auth_mock_session):
    """FastAPI test client with mocked database for auth tests."""
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import api

    async def override_get_db():
        yield auth_mock_session

    api.dependency_overrides[get_db] = override_get_db
    yield TestClient(api)
    del api.dependency_overrides[get_db]


@pytest.fixture(autouse=True)
def reset_auth_mock_db(auth_mock_session):
    """Reset auth mock database before each test."""
    auth_mock_session.reset_mock()

    # Default behavior for execute: return a mock result
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    auth_mock_session.execute.return_value = mock_result
    auth_mock_session.add = MagicMock()
    auth_mock_session.commit.return_value = None

    def side_effect_refresh(instance):
        instance.id = 1

    auth_mock_session.refresh.side_effect = side_effect_refresh
    return auth_mock_session


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
