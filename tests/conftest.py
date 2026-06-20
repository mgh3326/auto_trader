"""
Pytest configuration and common fixtures for auto-trader tests.
"""

import asyncio
import os
from collections.abc import Generator, Iterable
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

    # ROB-469 PR2: force tests onto NullPool. Production defaults to the async queue
    # pool (DB_POOL_CLASS=queue), but pytest-asyncio uses a fresh event loop PER TEST,
    # and the shared module-level engine (created once at import) would reuse pooled
    # connections bound to a now-closed loop → "attached to a different loop" errors.
    # NullPool checks out a fresh connection each time, avoiding that. Force-overwrite
    # (not setdefault) is required because env.example sets DB_POOL_CLASS=queue and is
    # loaded first. Tests that exercise build_engine() pool selection monkeypatch
    # DB_POOL_CLASS themselves.
    os.environ["DB_POOL_CLASS"] = "null"

    # Force disable Sentry during tests — prevent test-originated errors
    # from leaking to the real Sentry project (developer shell may have SENTRY_DSN set)
    os.environ["SENTRY_DSN"] = ""
    os.environ["ENVIRONMENT"] = "test"


_ensure_test_env()

from app.core.config import settings

MARKET_VALUATION_SOURCE_CHECK_NAME = "ck_market_valuation_snapshots_source"
MARKET_VALUATION_SOURCE_MODEL_CHECK_NAME = (
    "ck_market_valuation_snapshots_ck_market_valuation_snapshots_source"
)
MARKET_VALUATION_SOURCE_VALUES = ("naver_finance", "yahoo", "toss_openapi")

SNAPSHOT_KIND_CHECK_NAME = "ck_investment_snapshots_snapshot_kind"
SNAPSHOT_KIND_MODEL_CHECK_NAME = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)
SNAPSHOT_KIND_CHECK_NAMES = (
    SNAPSHOT_KIND_MODEL_CHECK_NAME,
    SNAPSHOT_KIND_CHECK_NAME,
)
SNAPSHOT_KIND_VALUES = (
    "portfolio",
    "market",
    "news",
    "symbol",
    "candidate_universe",
    "browser_probe",
    "invest_page",
    "journal",
    "watch_context",
    "naver_remote_debug",
    "toss_remote_debug",
    "llm_input_frozen",
    "pending_orders",
    "validated_run_card",
    "kr_market_ranking",
    "investor_flow",
)


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _check_constraint_sql(column_name: str, values: tuple[str, ...]) -> str:
    values_sql = ",".join(f"'{value}'" for value in values)
    return f"CHECK ({column_name} IN ({values_sql}))"


def _constraint_definitions_need_refresh(
    definitions: Iterable[str | None],
    required_values: tuple[str, ...],
) -> bool:
    definitions = list(definitions)
    if not definitions:
        return True
    return any(
        not all(value in (definition or "") for value in required_values)
        for definition in definitions
    )


async def _ensure_market_valuation_source_constraint(conn, sql_text) -> None:
    constraints = await conn.execute(
        sql_text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'market_valuation_snapshots'::regclass "
            "AND pg_get_constraintdef(oid) LIKE '%source%' "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if not _constraint_definitions_need_refresh(
        [row[1] for row in rows],
        MARKET_VALUATION_SOURCE_VALUES,
    ):
        return

    for name, _definition in rows:
        await conn.execute(
            sql_text(
                "ALTER TABLE market_valuation_snapshots "
                f"DROP CONSTRAINT IF EXISTS {_quote_ident(name)}"
            )
        )
    await conn.execute(
        sql_text(
            "ALTER TABLE market_valuation_snapshots "
            f"ADD CONSTRAINT {MARKET_VALUATION_SOURCE_CHECK_NAME} "
            f"{_check_constraint_sql('source', MARKET_VALUATION_SOURCE_VALUES)}"
        )
    )


async def _ensure_investment_snapshot_kind_constraint(conn, sql_text) -> None:
    names_sql = ",".join(f"'{name}'" for name in SNAPSHOT_KIND_CHECK_NAMES)
    constraints = await conn.execute(
        sql_text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE conrelid = 'review.investment_snapshots'::regclass "
            f"AND conname IN ({names_sql}) "
            "AND contype = 'c'"
        )
    )
    rows = list(constraints)
    if not _constraint_definitions_need_refresh(
        [row[1] for row in rows],
        SNAPSHOT_KIND_VALUES,
    ):
        return

    for name in SNAPSHOT_KIND_CHECK_NAMES:
        await conn.execute(
            sql_text(
                "ALTER TABLE review.investment_snapshots "
                f"DROP CONSTRAINT IF EXISTS {_quote_ident(name)}"
            )
        )
    await conn.execute(
        sql_text(
            "ALTER TABLE review.investment_snapshots "
            f"ADD CONSTRAINT {SNAPSHOT_KIND_CHECK_NAME} "
            f"{_check_constraint_sql('snapshot_kind', SNAPSHOT_KIND_VALUES)}"
        )
    )


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
    ):
        # Configure mock responses
        yield {
            "upbit": mock_upbit,
            "yahoo_download": mock_yahoo_download,
            "yahoo_ticker": mock_yahoo_ticker,
            "kis": mock_kis,
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
def _mock_kr_market_session_calendar(monkeypatch):
    """Use a deterministic lightweight KRX calendar in fast tests.

    Tests that need precise holiday behavior patch market_session._get_kr_calendar
    directly. The default fast gate only needs weekday/session arithmetic and must
    not pay the exchange_calendars XKRX construction cost in every xdist worker.
    """

    class _FastKrCalendar:
        tz = "Asia/Seoul"

        def _local(self, value):
            ts = pd.Timestamp(value)
            if ts.tz is None:
                return ts.tz_localize(self.tz)
            return ts.tz_convert(self.tz)

        def is_trading_minute(self, value):
            local = self._local(value)
            if local.weekday() >= 5:
                return False
            start = pd.Timestamp(local.date(), tz=self.tz) + pd.Timedelta(hours=9)
            end = pd.Timestamp(local.date(), tz=self.tz) + pd.Timedelta(
                hours=15, minutes=30
            )
            return start <= local < end

        def is_session(self, value):
            return self._local(value).weekday() < 5

    monkeypatch.setattr(
        "app.mcp_server.tooling.market_session._get_kr_calendar",
        lambda: _FastKrCalendar(),
    )


@pytest.fixture
def allow_tvscreener_http():
    """Opt out of the default tvscreener HTTP boundary block."""


@pytest.fixture(autouse=True)
def _block_tvscreener_http_boundary(request, monkeypatch):
    """Prevent accidental TradingView scanner HTTP calls in non-live tests."""
    if "allow_tvscreener_http" in request.fixturenames:
        return
    if request.node.get_closest_marker("live") and request.config.getoption(
        "--run-live"
    ):
        return

    from app.services.tvscreener_retry import TvScreenerError

    async def _raise_tvscreener_blocked(*_args, **_kwargs):
        raise TvScreenerError(
            "TvScreener HTTP is disabled during pytest; patch TvScreenerService "
            "with a fake or request allow_tvscreener_http for boundary/live tests."
        )

    monkeypatch.setattr(
        "app.services.tvscreener_service.TvScreenerService.fetch_with_retry",
        _raise_tvscreener_blocked,
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


# Markers for different test types
pytest_plugins = ["pytest_asyncio", "tests._investment_reports_helpers"]


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
    """Create a database session for testing with schema setup.

    CI runs pytest-xdist (``--dist=loadfile``), so multiple workers
    concurrently instantiate this fixture and the ``session`` fixture in
    ``tests/_investment_reports_helpers.py``. Both fixtures execute DDL on
    ``review.*`` tables; concurrent ALTER + CASCADE TRUNCATE collide on
    ``AccessExclusiveLock`` and deadlock (observed under CI as ROB-274 PR
    failure). We serialize the DDL phase here under the same advisory
    lock the helper uses (``INVESTMENT_REPORTS_TEST_LOCK_ID``) so the two
    fixtures take turns on the schema patch while the rest of the suite
    stays parallel. The lock is released BEFORE yielding so the per-test
    body runs unserialized.
    """
    from sqlalchemy import text

    import app.models  # noqa: F401
    import app.models.market_events  # noqa: F401
    from app.core.db import AsyncSessionLocal, engine
    from app.models.base import Base
    from tests._investment_reports_helpers import INVESTMENT_REPORTS_TEST_LOCK_ID

    async with engine.connect() as guard:
        await guard.execute(
            text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
        )
        try:
            async with engine.begin() as conn:
                # Create required schemas first (PostgreSQL-specific)
                for schema in ["paper", "research", "review"]:
                    await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
                await conn.run_sync(Base.metadata.create_all)
                # Idempotent column additions for schema drift between
                # create_all and migrations.
                await conn.execute(
                    text(
                        "ALTER TABLE market_events "
                        "ADD COLUMN IF NOT EXISTS currency TEXT"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE us_symbol_universe "
                        "ADD COLUMN IF NOT EXISTS is_common_stock BOOLEAN"
                    )
                )
                # ROB-430 PR-② — week_high_52_date added to the (persistent) KR
                # fundamentals snapshot table; create_all is a no-op on the existing
                # table, so patch the column in here (mirrors the alembic migration).
                await conn.execute(
                    text(
                        "ALTER TABLE invest_kr_fundamentals_snapshots "
                        "ADD COLUMN IF NOT EXISTS week_high_52_date DATE"
                    )
                )
                # ROB-440 PR3 — high_52w_date added to the (persistent) market
                # valuation snapshot table for US undervalued_breakout date-recency.
                # On a FRESH DB create_all already adds it (the ORM model declares
                # it), so only ALTER when genuinely missing — an unconditional
                # ALTER (even IF NOT EXISTS) takes an AccessExclusive lock on this
                # HOT table and widens the xdist DDL-vs-test deadlock window.
                mv_has_high_52w_date = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'market_valuation_snapshots' "
                            "AND column_name = 'high_52w_date'"
                        )
                    )
                ).first()
                if not mv_has_high_52w_date:
                    await conn.execute(
                        text(
                            "ALTER TABLE market_valuation_snapshots "
                            "ADD COLUMN high_52w_date DATE"
                        )
                    )
                # ROB-534 — Toss symbol master columns.
                for table, cols in [
                    (
                        "kr_symbol_universe",
                        [
                            ("security_type", "VARCHAR(20)"),
                            ("is_common_share", "BOOLEAN"),
                            ("listing_status", "VARCHAR(20)"),
                            ("list_date", "DATE"),
                            ("delist_date", "DATE"),
                            ("shares_outstanding", "NUMERIC(30, 0)"),
                            ("leverage_factor", "NUMERIC(12, 6)"),
                            ("krx_trading_suspended", "BOOLEAN"),
                            ("nxt_trading_suspended", "BOOLEAN"),
                            ("isin", "VARCHAR(20)"),
                            ("toss_master_updated_at", "TIMESTAMP WITH TIME ZONE"),
                        ],
                    ),
                    (
                        "us_symbol_universe",
                        [
                            ("security_type", "VARCHAR(20)"),
                            ("is_common_share", "BOOLEAN"),
                            ("listing_status", "VARCHAR(20)"),
                            ("list_date", "DATE"),
                            ("delist_date", "DATE"),
                            ("shares_outstanding", "NUMERIC(30, 0)"),
                            ("leverage_factor", "NUMERIC(12, 6)"),
                            ("isin", "VARCHAR(20)"),
                            ("toss_master_updated_at", "TIMESTAMP WITH TIME ZONE"),
                        ],
                    ),
                ]:
                    for col_name, col_type in cols:
                        has_col = (
                            await conn.execute(
                                text(
                                    f"SELECT 1 FROM information_schema.columns "
                                    f"WHERE table_name = '{table}' AND column_name = '{col_name}'"
                                )
                            )
                        ).first()
                        if not has_col:
                            await conn.execute(
                                text(
                                    f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                                )
                            )
                await _ensure_market_valuation_source_constraint(conn, text)
                # Recreate unique constraint if missing
                has_uq = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM pg_constraint "
                            "WHERE conrelid = 'market_valuation_snapshots'::regclass "
                            "AND conname = 'uq_market_valuation_snapshots_market_symbol_date_source'"
                        )
                    )
                ).first()
                if not has_uq:
                    await conn.execute(
                        text(
                            "ALTER TABLE market_valuation_snapshots "
                            "ADD CONSTRAINT uq_market_valuation_snapshots_market_symbol_date_source "
                            "UNIQUE (market, symbol, snapshot_date, source)"
                        )
                    )
                # ROB-443 PR1 — funding_rate added to the (persistent) crypto
                # screener snapshot table. Same fresh-DB/create_all logic: only
                # ALTER when genuinely missing to avoid an AccessExclusive lock.
                crypto_has_funding_rate = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'invest_crypto_screener_snapshots' "
                            "AND column_name = 'funding_rate'"
                        )
                    )
                ).first()
                if not crypto_has_funding_rate:
                    await conn.execute(
                        text(
                            "ALTER TABLE invest_crypto_screener_snapshots "
                            "ADD COLUMN funding_rate NUMERIC(12, 8)"
                        )
                    )
                # ROB-443 follow-up — OI / long-short columns on the (persistent)
                # crypto screener snapshot table. Same conditional-ALTER pattern.
                for _col, _ddl in (
                    ("open_interest_usd", "open_interest_usd NUMERIC(28, 2)"),
                    ("oi_change_24h", "oi_change_24h NUMERIC(10, 4)"),
                    (
                        "long_short_account_ratio",
                        "long_short_account_ratio NUMERIC(10, 4)",
                    ),
                ):
                    _has = (
                        await conn.execute(
                            text(
                                "SELECT 1 FROM information_schema.columns "
                                "WHERE table_name = 'invest_crypto_screener_snapshots' "
                                "AND column_name = :c"
                            ),
                            {"c": _col},
                        )
                    ).first()
                    if not _has:
                        await conn.execute(
                            text(
                                "ALTER TABLE invest_crypto_screener_snapshots "
                                f"ADD COLUMN {_ddl}"
                            )
                        )
                # ROB-284 — crypto_candles_1d migrates in-place from the
                # legacy (symbol, market) shape to the (instrument_id, time)
                # shape. The test DB picks up its schema from
                # ``Base.metadata.create_all`` which is no-op against an
                # existing table; if the legacy table is still present we
                # drop it here so create_all rebuilds it from the new ORM
                # model (``app.models.crypto_candles.CryptoCandle1d``).
                legacy_has_symbol = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'crypto_candles_1d' "
                            "AND column_name = 'symbol'"
                        )
                    )
                ).first()
                if legacy_has_symbol:
                    await conn.execute(
                        text("DROP TABLE IF EXISTS public.crypto_candles_1d CASCADE")
                    )
                # ROB-269 Phase 3 — snapshot metadata + 3-layer stale gate
                # layer (i). create_all is no-op for already-existing tables,
                # so we patch the six new columns + index + CHECK constraint
                # here so the persistent test DB picks them up without a full
                # alembic upgrade cycle.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_bundle_uuid UUID"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_policy_version TEXT"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_coverage_summary JSONB"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_freshness_summary JSONB"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS source_conflicts JSONB"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS unavailable_sources JSONB"
                    )
                )
                # ROB-318 Phase 3 (PR-B) — deterministic report diagnostics.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD COLUMN IF NOT EXISTS snapshot_report_diagnostics JSONB"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_investment_reports_snapshot_bundle_uuid "
                        "ON review.investment_reports (snapshot_bundle_uuid)"
                    )
                )
                # Postgres has no native ADD CONSTRAINT IF NOT EXISTS;
                # drop+recreate is idempotent and avoids a catalog-table probe.
                # ROB-269 Phase 3 (corrected by 20260519_rob269_p3a): explicit
                # ``IS NOT NULL`` guard prevents CHECK from accepting UNKNOWN
                # when ``overall`` is missing or JSON-null.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_reports_no_published_on_hard_stale"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_reports "
                        "ADD CONSTRAINT "
                        "ck_investment_reports_no_published_on_hard_stale "
                        "CHECK ("
                        "status <> 'published' "
                        "OR snapshot_freshness_summary IS NULL "
                        "OR ("
                        "(snapshot_freshness_summary->>'overall') IS NOT NULL "
                        "AND (snapshot_freshness_summary->>'overall') IN "
                        "('fresh','soft_stale','partial')"
                        "))"
                    )
                )
                # Avoid repeated AccessExclusive constraint refreshes on this
                # hot table while xdist workers are already running test bodies.
                await _ensure_investment_snapshot_kind_constraint(conn, text)
                # ROB-274 — proposal-state columns + operation-aware CHECKs on
                # investment_report_items. Mirrors the persistent-DB patch
                # pattern above; the canonical schema lives in migration
                # 20260520_rob274_p1_add_proposal_fields_to_report_items.py.
                for column_sql in (
                    "ADD COLUMN IF NOT EXISTS operation TEXT",
                    "ADD COLUMN IF NOT EXISTS target_ref JSONB",
                    "ADD COLUMN IF NOT EXISTS current_state JSONB",
                    "ADD COLUMN IF NOT EXISTS proposed_state JSONB",
                    "ADD COLUMN IF NOT EXISTS diff JSONB",
                    "ADD COLUMN IF NOT EXISTS apply_policy TEXT",
                    "ADD COLUMN IF NOT EXISTS decision_bucket TEXT",
                    "ADD COLUMN IF NOT EXISTS cited_symbol_report_uuid UUID",
                    "ADD COLUMN IF NOT EXISTS cited_dimension_report_uuids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
                    "ADD COLUMN IF NOT EXISTS cited_snapshot_uuids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[]",
                ):
                    await conn.execute(
                        text(f"ALTER TABLE review.investment_report_items {column_sql}")
                    )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_ck_investment_report_items_decision_bucket"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_decision_bucket"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT ck_investment_report_items_decision_bucket "
                        "CHECK ("
                        "decision_bucket IS NULL OR decision_bucket IN ("
                        "'new_buy_candidate','open_action','completed_or_existing','deferred_no_action','risk_watch'"
                        "))"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_investment_report_items_operation_kind "
                        "ON review.investment_report_items "
                        "(operation, item_kind, status)"
                    )
                )
                # operation + apply_policy CHECKs — drop+recreate is idempotent
                # and avoids a catalog-table probe.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_operation"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT ck_investment_report_items_operation "
                        "CHECK ("
                        "operation IS NULL OR operation IN ("
                        "'create','modify','cancel','keep','replace','review'"
                        "))"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_report_items_apply_policy"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT ck_investment_report_items_apply_policy "
                        "CHECK ("
                        "apply_policy IS NULL "
                        "OR apply_policy = 'requires_user_approval'"
                        ")"
                    )
                )
                # Rewrite watch-condition and watch-expiry CHECKs to the
                # operation-aware predicates. We drop the canonical name + the
                # hashed name the ROB-265 migration created under the
                # project's MetaData naming convention (see 20260520_rob274_p1
                # docstring).
                for canonical, hashed in (
                    (
                        "ck_investment_report_items_watch_has_condition",
                        "ck_investment_report_items_ck_investment_report_items_w_421e",
                    ),
                    (
                        "ck_investment_report_items_watch_has_expiry",
                        "ck_investment_report_items_ck_investment_report_items_w_fdaa",
                    ),
                ):
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.investment_report_items "
                            f'DROP CONSTRAINT IF EXISTS "{hashed}"'
                        )
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.investment_report_items "
                            f'DROP CONSTRAINT IF EXISTS "{canonical}"'
                        )
                    )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT "
                        "ck_investment_report_items_watch_has_condition "
                        "CHECK ("
                        "item_kind <> 'watch' "
                        "OR operation IN ('cancel','keep','review') "
                        "OR watch_condition IS NOT NULL"
                        ")"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_report_items "
                        "ADD CONSTRAINT "
                        "ck_investment_report_items_watch_has_expiry "
                        "CHECK ("
                        "item_kind <> 'watch' "
                        "OR operation IN ('cancel','keep','review') "
                        "OR valid_until IS NOT NULL"
                        ")"
                    )
                )
                # ROB-321 — add missing scalping columns if they are not present
                # on review.kis_mock_order_ledger in persistent test DB.
                for col_name, col_type in (
                    ("correlation_id", "TEXT"),
                    ("scalping_role", "TEXT"),
                    ("exit_reason", "TEXT"),
                    ("gross_pnl", "NUMERIC(20, 4)"),
                    ("net_pnl", "NUMERIC(20, 4)"),
                ):
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.kis_mock_order_ledger "
                            f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                        )
                    )
                # ROB-406 — extend kis_mock_order_ledger.lifecycle_state CHECK
                # to include 'cancelled'. create_all is no-op on the persistent
                # test table, so drop+recreate here; canonical schema lives in
                # migration <rev>_rob406_kis_mock_cancelled_state.py.
                await conn.execute(
                    text(
                        "ALTER TABLE review.kis_mock_order_ledger "
                        "DROP CONSTRAINT IF EXISTS "
                        "kis_mock_ledger_lifecycle_state_allowed"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.kis_mock_order_ledger "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_kis_mock_order_ledger_kis_mock_ledger_lifecycle_stat_8e10"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.kis_mock_order_ledger "
                        "ADD CONSTRAINT ck_kis_mock_order_ledger_kis_mock_ledger_lifecycle_stat_8e10 "
                        "CHECK (lifecycle_state IN ("
                        "'planned','previewed','submitted','accepted','pending',"
                        "'fill','reconciled','stale','failed','anomaly','cancelled'"
                        "))"
                    )
                )
                # ROB-403 — investment_watch_alerts: add conditions/combine/
                # threshold_high columns + extend operator CHECK to 'between'.
                # create_all is no-op on the persistent test table.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS conditions JSONB "
                        "NOT NULL DEFAULT '[]'::jsonb"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD COLUMN IF NOT EXISTS combine TEXT "
                        "NOT NULL DEFAULT 'and'"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_alerts_operator"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_alerts_ck_investment_watch_alerts_operator"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_operator "
                        "CHECK (operator IN ('above','below','between'))"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_alerts_combine"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_alerts_ck_investment_watch_alerts_combine"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_alerts "
                        "ADD CONSTRAINT ck_investment_watch_alerts_combine "
                        "CHECK (combine IN ('and'))"
                    )
                )
                # ROB-403 — investment_watch_events: between + threshold_high.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "ADD COLUMN IF NOT EXISTS threshold_high NUMERIC(20,8)"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_events_operator"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "DROP CONSTRAINT IF EXISTS "
                        "ck_investment_watch_events_ck_investment_watch_events_operator"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events "
                        "ADD CONSTRAINT ck_investment_watch_events_operator "
                        "CHECK (operator IN ('above','below','between'))"
                    )
                )
                # ROB-402 — action_mode auto_execute_mock on alerts + events.
                for _t in ("investment_watch_alerts", "investment_watch_events"):
                    _c = f"ck_{_t}_action_mode"
                    await conn.execute(
                        text(f"ALTER TABLE review.{_t} DROP CONSTRAINT IF EXISTS {_c}")
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.{_t} DROP CONSTRAINT IF EXISTS {_c}_{_c}"
                        )
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.{_t} DROP CONSTRAINT IF EXISTS ck_investment_watch_alerts_ck_investment_watch_alerts_a_646d"
                        )
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.{_t} DROP CONSTRAINT IF EXISTS ck_investment_watch_events_ck_investment_watch_events_a_05f0"
                        )
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.{_t} DROP CONSTRAINT IF EXISTS ck_investment_watch_events_ck_investment_watch_events_ac_6a20"
                        )
                    )
                    await conn.execute(
                        text(
                            f"ALTER TABLE review.{_t} ADD CONSTRAINT {_c} "
                            "CHECK (action_mode IN ('notify_only','preview_only',"
                            "'approval_required','auto_execute_mock'))"
                        )
                    )
                # ROB-402 — outcome executed on events.
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS ck_investment_watch_events_outcome"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events DROP CONSTRAINT IF EXISTS ck_investment_watch_events_ck_investment_watch_events_outcome"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.investment_watch_events ADD CONSTRAINT ck_investment_watch_events_outcome "
                        "CHECK (outcome IN ('notified','review_required','preview_attached',"
                        "'executed','expired','ignored','failed'))"
                    )
                )
                # ROB-405 Slice A — trade_journals: correlation_id + account_type 'mock'.
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "ADD COLUMN IF NOT EXISTS correlation_id TEXT"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "DROP CONSTRAINT IF EXISTS trade_journals_account_type"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "DROP CONSTRAINT IF EXISTS ck_trade_journals_trade_journals_account_type"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_journals "
                        "ADD CONSTRAINT trade_journals_account_type "
                        "CHECK (account_type IN ('live','paper','mock'))"
                    )
                )
                # ROB-473 — report_item_uuid column on live order ledgers
                await conn.execute(
                    text(
                        "ALTER TABLE review.kis_live_order_ledger "
                        "ADD COLUMN IF NOT EXISTS report_item_uuid UUID"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.live_order_ledger "
                        "ADD COLUMN IF NOT EXISTS report_item_uuid UUID"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_kis_live_ledger_report_item_uuid "
                        "ON review.kis_live_order_ledger (report_item_uuid)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_live_ledger_report_item_uuid "
                        "ON review.live_order_ledger (report_item_uuid)"
                    )
                )

                # ROB-568 — US FX PnL fields
                for table in (
                    "trade_journals",
                    "live_order_ledger",
                    "toss_live_order_ledger",
                    "trade_retrospectives",
                ):
                    for col, ddl in (
                        ("buy_fx_rate", "NUMERIC(18, 4)"),
                        ("sell_fx_rate", "NUMERIC(18, 4)"),
                        ("fx_pnl_krw", "NUMERIC(20, 4)"),
                        ("security_pnl_usd", "NUMERIC(20, 4)"),
                        ("security_pnl_krw", "NUMERIC(20, 4)"),
                        ("total_pnl_krw", "NUMERIC(20, 4)"),
                        ("fx_rate_source", "TEXT"),
                        ("fx_pnl_accuracy", "TEXT"),
                    ):
                        await conn.execute(
                            text(
                                f"ALTER TABLE review.{table} ADD COLUMN IF NOT EXISTS {col} {ddl}"
                            )
                        )

                # ROB-568 — TradeRetrospective account_mode constraint
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_retrospectives "
                        "DROP CONSTRAINT IF EXISTS ck_trade_retrospectives_account_mode"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_retrospectives "
                        "DROP CONSTRAINT IF EXISTS ck_trade_retrospectives_ck_trade_retrospectives_account_mode"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE review.trade_retrospectives "
                        "ADD CONSTRAINT ck_trade_retrospectives_account_mode "
                        "CHECK (account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live'))"
                    )
                )
                # B-1 (binance-phase1) — benchmark_return_bps on scalping_daily_reviews.
                # create_all is no-op on the persistent test table, so add here.
                await conn.execute(
                    text(
                        "ALTER TABLE scalping_daily_reviews "
                        "ADD COLUMN IF NOT EXISTS benchmark_return_bps NUMERIC(12, 4)"
                    )
                )
        finally:
            # Release the advisory lock BEFORE yielding so the per-test body
            # runs unserialized. The DDL above is durable + idempotent, so
            # the next worker that takes the lock is a no-op at the PG layer
            # but still needs the lock to safely co-exist with concurrent
            # TRUNCATEs from tests/_investment_reports_helpers.session.
            await guard.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def investment_reports_cleanup_lock(db_session):
    """Hold the investment-report cleanup lock for tests using ``db_session``.

    Most investment-report table tests use ``tests._investment_reports_helpers.session``.
    That fixture serializes its own body and cleanup because it truncates
    ``review.investment_reports`` and child tables after each test.

    A few cross-domain tests must use the global ``db_session`` fixture because
    they span ``review.investment_reports`` plus snapshot/stage tables. During
    xdist runs those tests can otherwise seed a report, commit it, and then race
    with the helper fixture cleanup running on another worker. The symptom is a
    flaky ``report_not_found``/``None`` read immediately after seeding.

    This fixture keeps those specific tests parallel-safe without serializing
    every global ``db_session`` user. Apply it with ``pytestmark =
    pytest.mark.usefixtures("investment_reports_cleanup_lock")`` in files that
    mix global ``db_session`` with investment-report rows.
    """
    from sqlalchemy import text

    from app.core.db import engine
    from tests._investment_reports_helpers import (
        INVESTMENT_REPORTS_TABLES,
        INVESTMENT_REPORTS_TEST_LOCK_ID,
    )

    async def _truncate_investment_report_tables() -> None:
        # Keep this cleanup aligned with tests._investment_reports_helpers.session:
        # only the investment-report table family is reset here. Snapshot/stage
        # tables use UUID-scoped test rows and are intentionally left alone.
        async with engine.begin() as cleanup:
            for table in reversed(INVESTMENT_REPORTS_TABLES):
                table_name = table.name  # type: ignore[attr-defined]
                await cleanup.execute(
                    text(
                        f'TRUNCATE TABLE review."{table_name}" RESTART IDENTITY CASCADE'
                    )
                )

    async with engine.connect() as guard:
        await guard.execute(
            text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
        )
        try:
            await db_session.rollback()
            await _truncate_investment_report_tables()
            yield db_session
            await db_session.rollback()
            await _truncate_investment_report_tables()
        finally:
            await guard.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )


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
