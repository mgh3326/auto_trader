"""
Pytest configuration and common fixtures for auto-trader tests.
"""

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
import pytest_asyncio

# Cross-worker mutex serialising the Alpaca-paper suites that mutate the shared
# `market_quote_snapshots` / `alpaca_paper_order_ledger` tables. See
# `_serialize_alpaca_paper_db_suites` below. Lives in the temp dir so every
# `pytest -n auto` worker process (same machine, same DB) contends on one file.
# Uses stdlib fcntl.flock (posix; the Linux CI + macOS dev boxes) — no extra
# dependency — and no-ops on the rare non-posix host.
_ALPACA_PAPER_DB_LOCK_PATH = (
    Path(tempfile.gettempdir()) / "auto_trader_alpaca_paper_db_suite.lock"
)


@contextlib.contextmanager
def _alpaca_paper_db_suite_lock() -> Generator[None]:
    try:
        import fcntl
    except ImportError:  # non-posix: cannot cross-process lock, run unserialised
        yield
        return
    with open(_ALPACA_PAPER_DB_LOCK_PATH, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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

    # ROB-638: hermetic guard — the analyze fetch-layer cache must NEVER touch a
    # real Redis from tests (a `make test` run on an operator host would poison
    # the live MCP cache with mock provider data). Force-disable; cache tests
    # patch analyze_cache._get_redis_client with a fake explicitly.
    os.environ["ANALYZE_FETCH_CACHE_ENABLED"] = "false"

    # ROB-688: same hermetic guard for the sector-peers cache — never touch a
    # real Redis from tests; cache tests inject a fake client explicitly.
    os.environ["NAVER_PEER_CACHE_ENABLED"] = "false"


_ensure_test_env()

from app.core.config import settings

# Re-export the schema-patch helpers + constants from ``tests._schema_bootstrap``
# for back-compat with tests/test_conftest_schema_patches.py (which imports them
# off ``tests.conftest``). The unified DDL lives in that module now.
from tests._schema_bootstrap import (  # noqa: F401
    MARKET_VALUATION_SOURCE_CHECK_NAME,
    MARKET_VALUATION_SOURCE_MODEL_CHECK_NAME,
    MARKET_VALUATION_SOURCE_VALUES,
    SNAPSHOT_KIND_CHECK_NAME,
    SNAPSHOT_KIND_CHECK_NAMES,
    SNAPSHOT_KIND_MODEL_CHECK_NAME,
    SNAPSHOT_KIND_VALUES,
    _check_constraint_sql,
    _constraint_definitions_need_refresh,
    _ensure_investment_snapshot_kind_constraint,
    _ensure_market_valuation_source_constraint,
    _quote_ident,
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


@pytest.fixture(autouse=True)
def _serialize_alpaca_paper_db_suites(request):
    """Serialize Alpaca-paper suites that mutate shared DB tables across workers.

    The Alpaca-paper test files seed committed rows into two globally shared
    tables (`market_quote_snapshots`, `alpaca_paper_order_ledger`) and clean up
    with broad committed DELETEs keyed on values that are IDENTICAL across every
    such suite — the ``"AAPL"`` quote symbol and the server-derived
    ``rob73-``/``rob74-crypto-`` ledger-key prefixes (which are server-owned, so
    a test cannot make them unique). Under CI's ``pytest -n auto
    --dist=loadfile`` these sibling files run in separate workers against one
    database, so a peer's committed cleanup can delete another running suite's
    live rows between insert and read — surfacing as flaky
    ``no_trusted_snapshot`` / ``LedgerNotFoundError`` failures. (Latent on main;
    duration-based ``--splits`` kept the hostile files in separate DB jobs.)

    A cross-worker file lock lets only one such suite touch those tables at a
    time. It is *outer* to each file's own ``_clean`` autouse fixture (conftest
    autouse fixtures wrap module autouse fixtures), so a peer never runs while
    another suite's committed cleanup is in flight. The intra-test
    ``asyncio.gather`` concurrency the exactly-once claim tests rely on still
    runs inside the lock (one process holds it for the whole test)."""
    path = str(getattr(request.node, "fspath", "") or "")
    if "alpaca_paper" not in path and "paper_approval_packet" not in path:
        yield
        return
    with _alpaca_paper_db_suite_lock():
        yield


@pytest.fixture(autouse=True)
def _isolate_kis_circuit_breaker(monkeypatch):
    # ROB-699: the KIS circuit breaker is a per-process singleton, enabled by
    # default. Force it OFF + reset it for every test so the existing KIS suite
    # is byte-identical passthrough and no connect/read errors leak across tests.
    # Breaker tests inject their own enabled breaker (settings_obj / cb._breaker),
    # which ignores this global flag.
    from app.services.brokers.kis import circuit_breaker as _cb

    monkeypatch.setattr(settings, "kis_circuit_breaker_enabled", False, raising=False)
    _cb.reset_kis_circuit_breaker()
    yield
    _cb.reset_kis_circuit_breaker()


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


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _bootstrap_test_schema():
    """ROB-723: apply the test schema exactly once, before any test body.

    Under xdist ``--dist=loadfile`` every worker enters this session-scoped
    autouse fixture before running its first test. The first worker to win the
    advisory lock runs the full DDL while all other workers block on the lock
    (barrier); subsequent workers see the content-hash sentinel and skip all
    DDL. Result: schema DDL (AccessExclusive) never overlaps another worker's
    test-body SELECT, closing the deadlock window.
    """
    from sqlalchemy import text

    from app.core.db import engine
    from tests._db_retry import run_with_deadlock_retry
    from tests._investment_reports_helpers import INVESTMENT_REPORTS_TEST_LOCK_ID
    from tests._schema_bootstrap import apply_test_schema, schema_content_hash

    wanted = schema_content_hash()

    async def _bootstrap_once() -> None:
        async with engine.connect() as guard:
            await guard.execute(
                text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "CREATE TABLE IF NOT EXISTS public._pytest_schema_ready ("
                            "content_hash TEXT PRIMARY KEY, "
                            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                        )
                    )
                    already = (
                        await conn.execute(
                            text(
                                "SELECT 1 FROM public._pytest_schema_ready "
                                "WHERE content_hash = :h"
                            ),
                            {"h": wanted},
                        )
                    ).first()
                    if already:
                        return
                    await apply_test_schema(conn)
                    await conn.execute(text("DELETE FROM public._pytest_schema_ready"))
                    await conn.execute(
                        text(
                            "INSERT INTO public._pytest_schema_ready (content_hash) "
                            "VALUES (:h)"
                        ),
                        {"h": wanted},
                    )
            finally:
                await guard.execute(
                    text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )

    await run_with_deadlock_retry(_bootstrap_once)
    yield


# Database fixtures for integration tests


@pytest_asyncio.fixture
async def db_session():
    """Async session against the shared test_db.

    Schema is owned by the session-scoped ``_bootstrap_test_schema`` barrier
    (ROB-723); this fixture performs no DDL — that is what previously overlapped
    other xdist workers' test bodies and deadlocked.
    """
    from app.core.db import AsyncSessionLocal

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
    from tests._db_retry import run_with_deadlock_retry
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
            await run_with_deadlock_retry(
                _truncate_investment_report_tables,
                rollback=db_session.rollback,
            )
            yield db_session
            await db_session.rollback()
            await run_with_deadlock_retry(
                _truncate_investment_report_tables,
                rollback=db_session.rollback,
            )
        finally:
            await guard.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )


@pytest_asyncio.fixture
async def retrospective_action_control_lock():
    """Serialize tests that mutate the global retrospective-action authority.

    The ROB-878/880 migration and cutover contracts intentionally change the
    singleton control row and, in a few cases, rebuild its tables.  Xdist
    workers share one PostgreSQL database, so those tests must not observe one
    another's temporary canonical mode or DDL state.
    """
    from sqlalchemy import text

    from app.core.db import engine

    # Distinct from the production cutover lock (878_880_001), which the
    # cutover contract tests must still be able to acquire while holding this
    # outer test-isolation lock.
    retrospective_action_control_test_lock_id = 878_880_999
    async with engine.connect() as guard:
        await guard.execute(
            text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": retrospective_action_control_test_lock_id},
        )
        try:
            yield
        finally:
            await guard.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": retrospective_action_control_test_lock_id},
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


@pytest_asyncio.fixture
async def toss_ledger_cleanup_lock():
    """Serialize tests that globally delete/scan ``review.toss_live_order_ledger``.

    Several toss-ledger test files run an autouse pre-clean that issues a
    whole-table ``delete(TossLiveOrderLedger)`` and assert on ledger rows they
    just committed. Under xdist those files land on different workers and nuke
    each other's in-flight rows mid-test (2026-07-11, PR #1500 CI: a replay
    row vanished between the seeding call and the idempotency re-check —
    ROB-834). Same remedy as ``investment_reports_cleanup_lock``: hold a
    Postgres advisory lock for the duration of each test in the marked files,
    serializing only the toss-ledger family against itself. Apply with
    ``pytestmark = pytest.mark.usefixtures("toss_ledger_cleanup_lock")``.
    """
    from sqlalchemy import text

    from app.core.db import engine

    TOSS_LEDGER_TEST_LOCK_ID = 265_202_711

    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": TOSS_LEDGER_TEST_LOCK_ID},
        )
        try:
            yield
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": TOSS_LEDGER_TEST_LOCK_ID},
            )


@pytest_asyncio.fixture
async def binance_demo_reservation_lock():
    """Serialize files that COMMIT open-root rows to ``binance_demo_order_ledger``.

    ROB-844 makes ``reserve_root_planned`` commit the planned root so the claim
    is durable and visible across processes. Consequently every executor test
    running ``confirm=True`` leaves a committed open-root behind, and the
    global open-*root* cap is a table-wide count. A concurrency test that asserts
    "global cap N admits exactly one" therefore races any other file committing
    open roots on another xdist worker (the ``--dist=loadfile`` shared-test_db
    hazard, ROB-842). Same remedy as ``toss_ledger_cleanup_lock``: hold a
    Postgres advisory lock for each test in the marked files so the
    open-root-committing binance-demo family is serialized against itself. Apply
    with ``pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")``.

    Distinct key from the production reservation advisory lock, so it never
    blocks the reservation path under test — it only serializes test files.
    """
    from sqlalchemy import text

    from app.core.db import engine

    BINANCE_DEMO_RESERVATION_TEST_LOCK_ID = 844_000_844

    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": BINANCE_DEMO_RESERVATION_TEST_LOCK_ID},
        )
        try:
            yield
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": BINANCE_DEMO_RESERVATION_TEST_LOCK_ID},
            )


@pytest_asyncio.fixture
async def binance_demo_smoke_ledger_isolation(binance_demo_reservation_lock):
    """Serialize and remove rows committed by the two real-ledger smoke tests.

    The smoke kernels intentionally commit every lifecycle transition. Their
    randomized ``rob298-*`` ids previously escaped the ordinary ``db_session``
    rollback and changed table-wide count assertions on another xdist worker.
    """
    from sqlalchemy import delete, or_

    from app.core.db import AsyncSessionLocal
    from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger

    async def _cleanup() -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(BinanceDemoOrderLedger).where(
                    or_(
                        BinanceDemoOrderLedger.client_order_id.like("rob298-%"),
                        BinanceDemoOrderLedger.client_order_id.like("rob-298-fut-%"),
                    )
                )
            )
            await session.commit()

    await _cleanup()
    yield
    await _cleanup()
