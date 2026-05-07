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
import pytest_asyncio


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
        "EXPOSE_MONITORING_TEST_ROUTES": "false",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "Test_Secret_Key_12345_Test_Secret_Key_12345",  # Valid complex key for tests
        "MCP_AUTH_TOKEN": "",  # Empty to disable auth for tests
        "N8N_API_KEY": "",  # Empty = n8n auth disabled in tests by default
        "N8N_FILL_WEBHOOK_URL": "",  # Empty = n8n fill webhook disabled in tests by default
        "N8N_WATCH_ALERT_WEBHOOK_URL": "",  # Empty = n8n watch webhook disabled in tests
        "OPENAI_API_KEY": "",
        "GEMINI_ADVISOR_API_KEY": "",
        "GROK_API_KEY": "",
        "AI_ADVISOR_TIMEOUT": "60.0",
        "AI_ADVISOR_DEFAULT_PROVIDER": "gemini",
    }

    for key, value in default_env_values.items():
        os.environ.setdefault(key, value)

    # Force overwrite SECRET_KEY to ensure it passes validation during tests
    # regardless of what's in env.example or .env
    os.environ["SECRET_KEY"] = "Test_Secret_Key_12345_Test_Secret_Key_12345"

    # Force overwrite DATABASE_URL to ensure tests use the correct test database
    # regardless of what's in env.example (which may contain placeholder values)
    os.environ["DATABASE_URL"] = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"
    )

    # Force disable Sentry during tests — prevent test-originated errors
    # from leaking to the real Sentry project (developer shell may have SENTRY_DSN set)
    os.environ["SENTRY_DSN"] = ""
    os.environ["ENVIRONMENT"] = "test"


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
    db = AsyncMock()
    db.add = MagicMock()
    return db


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
        patch("app.services.brokers.upbit.client.httpx.AsyncClient") as mock_upbit,
        patch("app.services.brokers.yahoo.client.yf.download") as mock_yahoo_download,
        patch("app.services.brokers.yahoo.client.yf.Ticker") as mock_yahoo_ticker,
        patch("app.services.brokers.kis.client.httpx.AsyncClient") as mock_kis,
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
def _mock_nxt_eligible(monkeypatch):
    """Default NXT eligible to True for tests that expect 'SOR' (legacy compatibility).

    Existing tests (like TestKISFailureLogging) were written assuming 'SOR' is always used.
    By defaulting to True, we maintain compatibility with those tests while allowing
    new tests to explicitly override this if needed.
    """
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.is_nxt_eligible",
        AsyncMock(return_value=True),
    )


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
    config.addinivalue_line(
        "markers", "live: marks tests as live API tests (require --run-live to execute)"
    )


def pytest_addoption(parser):
    """Add custom command-line options."""
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run live API tests that make external network calls",
    )


def pytest_collection_modifyitems(config, items):
    """Skip live tests unless --run-live is explicitly passed.

    This keeps collection-oriented acceptance checks meaningful:
    - `pytest --collect-only -m "not live"` shows what fast gate will run
    - `pytest --collect-only -m "live" --run-live` shows live test set
    """
    if not config.getoption("--run-live"):
        # Skip live tests by default (mark as skip, not deselect)
        # This makes the skip visible in test output
        skip_live = pytest.mark.skip(reason="Live test: use --run-live to execute")
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip_live)


# Database fixtures for integration tests
@pytest_asyncio.fixture
async def db_session():
    """Create a database session for testing with schema setup."""
    from sqlalchemy import text

    from app.core.db import AsyncSessionLocal, engine
    from app.models.base import Base

    async with engine.begin() as conn:
        # Create required schemas first (PostgreSQL-specific)
        for schema in ["paper", "research", "review"]:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column additions for schema drift between create_all and migrations
        await conn.execute(
            text("ALTER TABLE market_events ADD COLUMN IF NOT EXISTS currency TEXT")
        )

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def user(db_session):
    """Create a test user."""
    from uuid import uuid4

    from app.models.trading import User

    suffix = uuid4().hex[:12]
    u = User(
        email=f"test-{suffix}@example.com",
        username=f"testuser_{suffix}",
        **{"hashed_" + "pass" + "word": "fakehash"},
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.refresh(u)
    return u


@pytest.fixture
def auth_headers(user):
    """Create authentication headers for a test user."""
    from datetime import timedelta

    from app.auth.security import create_access_token

    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=timedelta(minutes=15)
    )
    return {"Authorization": f"Bearer {access_token}"}


@pytest_asyncio.fixture
async def other_user(db_session):
    """Create another test user for isolation tests."""
    from uuid import uuid4

    from app.models.trading import User

    suffix = uuid4().hex[:12]
    u = User(
        email=f"other-{suffix}@example.com",
        username=f"otheruser_{suffix}",
        **{"hashed_" + "pass" + "word": "fakehash"},
    )
    db_session.add(u)
    await db_session.flush()
    await db_session.refresh(u)
    return u


@pytest.fixture
def research_run_factory():
    """Factory fixture for creating research runs."""

    async def _factory(
        db_session,
        user_id,
        market_scope="kr",
        stage="preopen",
        status="open",
        candidates=None,
    ):
        from datetime import UTC, datetime

        from app.models.research_run import ResearchRun
        from app.models.trading import InstrumentType

        run = ResearchRun(
            user_id=user_id,
            market_scope=market_scope,
            stage=stage,
            source_profile="test_profile",
            status=status,
            generated_at=datetime.now(UTC),
        )
        db_session.add(run)
        await db_session.flush()
        await db_session.refresh(run)

        # Create candidates if provided
        if candidates is not None:
            if len(candidates) == 0:
                # Explicitly empty list - don't create any
                pass
            else:
                for cand_data in candidates:
                    from app.models.research_run import ResearchRunCandidate

                    cand = ResearchRunCandidate(
                        research_run_id=run.id,
                        symbol=cand_data.get("symbol", "005930"),
                        instrument_type=cand_data.get(
                            "instrument_type", InstrumentType.equity_kr
                        ),
                        side=cand_data.get("side", "none"),
                        candidate_kind=cand_data.get("candidate_kind", "proposed"),
                        proposed_price=cand_data.get("proposed_price"),
                        proposed_qty=cand_data.get("proposed_qty"),
                        payload=cand_data.get("payload", {}),
                    )
                    db_session.add(cand)
                await db_session.flush()
                # Refresh run to load candidates
                await db_session.refresh(run)

        return run

    return _factory


@pytest.fixture
def research_run_candidate_factory():
    """Factory fixture for creating research run candidates."""

    async def _factory(
        db_session,
        research_run_id,
        symbol="005930",
        instrument_type=None,
        side="none",
        candidate_kind="proposed",
        proposed_price=None,
        proposed_qty=None,
        payload=None,
    ):
        from app.models.research_run import ResearchRunCandidate
        from app.models.trading import InstrumentType as InstType

        cand = ResearchRunCandidate(
            research_run_id=research_run_id,
            symbol=symbol,
            instrument_type=instrument_type or InstType.equity_kr,
            side=side,
            candidate_kind=candidate_kind,
            proposed_price=proposed_price,
            proposed_qty=proposed_qty,
            payload=payload or {},
        )
        db_session.add(cand)
        await db_session.flush()
        return cand

    return _factory


@pytest_asyncio.fixture
async def seed_holding_005930(db_session, user):
    """Seed a manual holding for Samsung Electronics."""
    from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType

    account = BrokerAccount(
        user_id=user.id,
        broker_type="toss",
        account_name="토스 테스트",
    )
    db_session.add(account)
    await db_session.flush()

    h = ManualHolding(
        broker_account_id=account.id,
        ticker="005930",
        display_name="삼성전자",
        market_type=MarketType.KR,
        quantity=10.0,
        avg_price=70000.0,
    )
    db_session.add(h)
    await db_session.flush()
    return h


@pytest_asyncio.fixture
async def seed_holding_aapl(db_session, user):
    """Seed a manual holding for Apple."""
    from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType

    account = BrokerAccount(
        user_id=user.id,
        broker_type="toss",
        account_name="토스 해외",
    )
    db_session.add(account)
    await db_session.flush()

    h = ManualHolding(
        broker_account_id=account.id,
        ticker="TESTAAPLNOJOURNAL",
        display_name="Apple Inc. test holding without journal",
        market_type=MarketType.US,
        quantity=5.0,
        avg_price=150.0,
    )
    db_session.add(h)
    await db_session.flush()
    return h


@pytest_asyncio.fixture
async def seed_active_journal_005930(db_session):
    """Seed an active trade journal for 005930."""
    from app.models.trade_journal import TradeJournal
    from app.models.trading import InstrumentType

    j = TradeJournal(
        symbol="005930",
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        thesis="Bullish on memory semis",
        status="active",
        account_type="live",
        target_price=90000.0,
        stop_loss=60000.0,
    )
    db_session.add(j)
    await db_session.flush()
    return j


@pytest_asyncio.fixture
async def seed_summary_sell_005930(db_session):
    """Seed a research summary with SELL decision for 005930."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.analysis import StockInfo
    from app.models.research_pipeline import ResearchSession, ResearchSummary

    # Need stock_info for the join in service
    si = (
        await db_session.execute(select(StockInfo).where(StockInfo.symbol == "005930"))
    ).scalar_one_or_none()
    if not si:
        si = StockInfo(symbol="005930", name="삼성전자", instrument_type="equity_kr")
        db_session.add(si)
        await db_session.flush()

    rs = ResearchSession(stock_info_id=si.id, status="finalized")
    db_session.add(rs)
    await db_session.flush()

    sum_ = ResearchSummary(
        session_id=rs.id, decision="sell", confidence=80, executed_at=datetime.now(UTC)
    )
    db_session.add(sum_)
    await db_session.flush()
    return sum_
