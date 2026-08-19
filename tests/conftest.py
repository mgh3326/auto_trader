"""
Pytest configuration and common fixtures for auto-trader tests.
"""

import asyncio
import contextlib
import os
import tempfile
import time
from collections import Counter
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
import pytest_asyncio

from tests import _external_http_boundary as external_http_boundary
from tests import _provider_boundaries as provider_boundaries
from tests._run_owned_database import (
    DATABASE_NAME_ENV as _XDIST_DATABASE_NAME_ENV,
)
from tests._run_owned_database import (
    DEFAULT_TEST_DATABASE_URL as _DEFAULT_TEST_DATABASE_URL,
)
from tests._run_owned_database import (
    configure_test_database_environment,
    drop_run_owned_database,
    ensure_run_owned_database,
    uses_run_owned_database,
    uses_shared_test_database,
)

# Compatibility mutex for the explicit shared-DB opt-in. Normal serial and
# xdist runs own separate databases and never take this lock. It lives in the
# temp dir so opted-in workers using the same database contend on one file.
# Uses stdlib fcntl.flock (posix; the Linux CI + macOS dev boxes) — no extra
# dependency — and no-ops on the rare non-posix host.
_ALPACA_PAPER_DB_LOCK_PATH = (
    Path(tempfile.gettempdir()) / "auto_trader_alpaca_paper_db_suite.lock"
)
_DATABASE_FIXTURE_NAMES = frozenset(
    {
        "db_session",
        "session",
        "committed_investment_reports_session",
        "investment_reports_cleanup_lock",
        "retrospective_action_control_lock",
        "toss_ledger_cleanup_lock",
        "binance_demo_reservation_lock",
        "binance_demo_smoke_ledger_isolation",
    }
)
_SCHEMA_METRICS_KEY = pytest.StashKey[list[dict[str, object]]]()
_EXTERNAL_HTTP_KEY = pytest.StashKey["Counter[str]"]()
_KIS_DEFAULT_SCOPE_PARTS = (
    "/brokers/kis/",
    "/services/brokers/kis/",
    "/services/order_proposals/",
    "/test_kis",
    "/test_mcp_order",
    "/test_mcp_place_order",
    "/test_nxt",
    "/test_order",
    "/test_services_kis",
)
_KR_CALENDAR_SCOPE_PARTS = (
    "market_session",
    "session_calendar",
    "/test_daily_scan.py",
    "/test_market_events",
    "/test_mcp_",
    "/test_preopen",
    "/test_screener",
    "/services/market_events/",
)
_AUTH_DB_SCOPE_PARTS = (
    "/routers/",
    "/test_admin",
    "/test_agent_callback",
    "/test_api",
    "/test_auth",
    "/test_dependencies",
    "/test_invest_api",
    "/test_main",
    "/test_middleware",
    "/test_routers",
    "/test_web",
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
        "DATABASE_URL": _DEFAULT_TEST_DATABASE_URL,
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

    # Select a run-owned database name without opening a connection. Serial
    # pytest and every xdist worker get an exact, validated target. Pure unit
    # and collect-only runs never cross the PostgreSQL boundary.
    configure_test_database_environment()

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


@pytest.fixture
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


@pytest.fixture
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
    runs inside the lock (one process holds it for the whole test).

    ROB-954 round-2: ``test_trade_retrospective_pending.py`` doesn't seed the
    shared ``alpaca_paper_order_ledger`` table itself, but its
    ``account_mode=None`` scans read it with exact ``total_pending ==`` counts
    over a wide ``2000-01-01``..``2100-01-01`` window — a peer alpaca_paper
    suite committing a ``rob73-``/``rob74-crypto-`` row mid-test inflates that
    count exactly like the write/write races above, so it needs the same
    cross-worker serialization even though it is read-mostly here. (Do not
    also wrap this file's own cleanup in ``_alpaca_paper_db_suite_lock()`` —
    ``fcntl.flock`` is not reentrant across separate ``open()`` calls even
    within one process, so nesting it under this fixture would deadlock.)"""
    if uses_run_owned_database():
        yield
        return
    with _alpaca_paper_db_suite_lock():
        yield


@pytest.fixture
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


@pytest.fixture
def allow_external_providers():
    """Opt out of the provider-seam stubs (the transport backstop still applies)."""


@pytest.fixture(autouse=True)
def _block_external_provider_calls(request, monkeypatch):
    """ROB-1296: stop each external provider at its call-root, not at the socket.

    ``_block_external_http_boundary`` below is the fail-closed backstop; this is
    the actual fix. Without it a default ``not live`` run still *built* requests
    to openapi.koreainvestment.com, api.upbit.com, api.finnhub.io, data.krx.co.kr,
    finance.naver.com, api.coingecko.com and open.er-api.com — the backstop
    merely refused them one layer later, which moves the leak rather than closing
    it. With both in place the boundary counter should list only this suite's own
    ``.invalid`` probes.

    Each seam raises the transport error an unreachable host already produced, so
    caller behaviour is unchanged and no payload is invented. See
    tests/_provider_boundaries.py for the seam list and the reasoning.
    """
    if provider_boundaries.OPT_OUT_FIXTURE in set(request.fixturenames) or (
        request.node.get_closest_marker("live")
        and request.config.getoption("--run-live", default=False)
    ):
        yield
        return

    undo = provider_boundaries.install(monkeypatch)
    try:
        yield
    finally:
        undo()


@pytest.fixture
def allow_external_http():
    """Opt out of the default external-HTTP boundary block.

    For a test that must drive httpx's real-network transport (typically with its
    own monkeypatched stand-in). Opting out does not make the network reachable:
    the ROB-1880 socket guard still refuses any non-loopback address.
    """


@pytest.fixture(autouse=True)
def _block_external_http_boundary(request, monkeypatch):
    """ROB-1296: turn a leaked external request into a clean, counted error.

    The socket guard already refuses these connections, so this changes *how*
    they fail, not *whether* they leave. It patches only httpx's real-network
    transports and the ``requests`` adapter, and raises the exception type a
    blocked connect already produces -- ``httpx.ConnectError`` /
    ``requests.ConnectionError`` -- so every ``except httpx.HTTPError``, retry
    and fail-open branch behaves exactly as it does today. No success is
    fabricated and no test's contract moves.

    ``ASGITransport`` (FastAPI ``TestClient``), ``WSGITransport``,
    ``MockTransport``, respx and any custom transport are untouched, and loopback
    stays reachable for local test servers.

    This is a backstop, not a substitute for mocking: it stops the default suite
    from *attempting* an external request, but a provider call that matters
    should still be stubbed at its call site (see ``_patch_us_finnhub_fanout`` in
    tests/test_mcp_fundamentals_tools.py). Blocked hosts are counted and printed
    in the terminal summary so a newly under-mocked provider shows up instead of
    quietly failing soft. tests/test_rob1296_external_http_boundary.py pins all
    of this.
    """
    if not external_http_boundary.boundary_is_active(
        has_live_marker=request.node.get_closest_marker("live") is not None,
        run_live=bool(request.config.getoption("--run-live", default=False)),
        fixturenames=request.fixturenames,
    ):
        return

    import httpx
    import requests

    original_async = httpx.AsyncHTTPTransport.handle_async_request
    original_sync = httpx.HTTPTransport.handle_request
    original_requests_send = requests.adapters.HTTPAdapter.send

    async def _blocked_async(self, request_, *args, **kwargs):
        host = request_.url.host
        if external_http_boundary.is_loopback_host(host):
            return await original_async(self, request_, *args, **kwargs)
        external_http_boundary.record_block(host)
        raise httpx.ConnectError(
            f"{external_http_boundary.MESSAGE} [{host}]", request=request_
        )

    def _blocked_sync(self, request_, *args, **kwargs):
        host = request_.url.host
        if external_http_boundary.is_loopback_host(host):
            return original_sync(self, request_, *args, **kwargs)
        external_http_boundary.record_block(host)
        raise httpx.ConnectError(
            f"{external_http_boundary.MESSAGE} [{host}]", request=request_
        )

    def _blocked_requests(self, request_, *args, **kwargs):
        host = requests.compat.urlparse(request_.url).hostname or ""
        if external_http_boundary.is_loopback_host(host):
            return original_requests_send(self, request_, *args, **kwargs)
        external_http_boundary.record_block(host)
        raise requests.ConnectionError(f"{external_http_boundary.MESSAGE} [{host}]")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async
    )
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_sync)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked_requests)


@pytest.fixture
def allow_kis_daily_candle_fetch():
    """Opt out of the default KIS daily-candle boundary block."""


@pytest.fixture(autouse=True)
def _block_kis_daily_candle_boundary(request, monkeypatch):
    """ROB-1296: keep the daily-candle cache-miss fallback off the network.

    ``_cache_first_kr`` (and the daily-candle sync service) fall back to
    ``fetch_kr_daily_unclamped`` -> KIS whenever the DB cache is cold, which it
    always is in a fresh test database. Callers treat that fetch as best-effort
    and fall back to whatever rows they already had, so the failure never
    surfaced — it just cost a real request to openapi.koreainvestment.com from
    tests that only care about names, freshness flags, or price targets.

    Raising the same class of error the fallback already handles keeps observable
    behaviour identical while removing the socket. A test that genuinely covers
    the KIS fetch path requests ``allow_kis_daily_candle_fetch`` (and supplies
    its own stub).
    """
    if "allow_kis_daily_candle_fetch" in request.fixturenames:
        return
    if request.node.get_closest_marker("live") and request.config.getoption(
        "--run-live"
    ):
        return

    async def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "KIS daily-candle fetch is disabled during pytest; stub "
            "fetch_kr_daily_unclamped or request allow_kis_daily_candle_fetch "
            "for boundary/live tests."
        )

    monkeypatch.setattr(
        "app.services.daily_candles.kis_daily_fetcher.fetch_kr_daily_unclamped",
        _blocked,
    )


@pytest.fixture
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
def auth_test_client(auth_mock_session, reset_auth_mock_db):
    """FastAPI test client with mocked database for auth tests."""
    assert reset_auth_mock_db is auth_mock_session
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import api

    async def override_get_db():
        yield auth_mock_session

    api.dependency_overrides[get_db] = override_get_db
    yield TestClient(api)
    del api.dependency_overrides[get_db]


@pytest.fixture
def reset_auth_mock_db(auth_mock_session):
    """Reset auth mock database before each test."""
    auth_mock_session.reset_mock(side_effect=True)

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


# Markers for different test types.
pytest_plugins = ["pytest_asyncio", "tests._investment_reports_helpers"]


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.stash[_SCHEMA_METRICS_KEY] = []
    config.stash[_EXTERNAL_HTTP_KEY] = Counter()
    # Process-global counter: reset per session so a nested or repeated session
    # in the same interpreter reports its own traffic, not the parent's.
    external_http_boundary.reset()
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")
    config.addinivalue_line(
        "markers", "live: marks tests as live API tests (require --run-live to execute)"
    )


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Forward per-worker schema metrics and HTTP-boundary evidence upward."""

    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        worker_output["auto_trader_schema_metrics"] = session.config.stash[
            _SCHEMA_METRICS_KEY
        ]
        worker_output["auto_trader_external_http_blocks"] = (
            external_http_boundary.snapshot()
        )
        return
    for host, count in external_http_boundary.snapshot().items():
        session.config.stash[_EXTERNAL_HTTP_KEY][host] += count


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:  # noqa: ARG001
    metrics = node.workeroutput.get("auto_trader_schema_metrics", [])
    node.config.stash[_SCHEMA_METRICS_KEY].extend(metrics)
    for host, count in node.workeroutput.get(
        "auto_trader_external_http_blocks", {}
    ).items():
        node.config.stash[_EXTERNAL_HTTP_KEY][str(host)] += int(count)


def pytest_terminal_summary(terminalreporter) -> None:
    blocked = dict(terminalreporter.config.stash[_EXTERNAL_HTTP_KEY])
    terminalreporter.write_line(external_http_boundary.format_summary(blocked))

    metrics = terminalreporter.config.stash[_SCHEMA_METRICS_KEY]
    if not metrics:
        return
    applied = sum(bool(metric["schema_applied"]) for metric in metrics)
    schema_seconds = sum(float(metric["schema_seconds"]) for metric in metrics)
    database_seconds = sum(float(metric["database_seconds"]) for metric in metrics)
    terminalreporter.write_line(
        "test schema bootstrap: "
        f"databases={len(metrics)} applied={applied} "
        f"schema_seconds={schema_seconds:.2f} "
        f"database_seconds={database_seconds:.2f}"
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
    skip_live = pytest.mark.skip(reason="Live test: use --run-live to execute")
    for item in items:
        # A test requesting a real database fixture is integration by
        # construction. Keep that invariant centralized so legacy file-level
        # ``unit`` marks cannot accidentally put DB tests in the fast loop.
        if _DATABASE_FIXTURE_NAMES.intersection(item.fixturenames):
            item.add_marker(pytest.mark.integration)

        if not config.getoption("--run-live") and item.get_closest_marker("live"):
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _scoped_test_defaults(request: pytest.FixtureRequest) -> None:
    """Activate compatibility defaults only for their owning test areas."""

    path = str(getattr(request.node, "path", "") or "")
    if any(part in path for part in _KIS_DEFAULT_SCOPE_PARTS):
        request.getfixturevalue("_mock_nxt_eligible")
        request.getfixturevalue("_isolate_kis_circuit_breaker")
    if any(part in path for part in _KR_CALENDAR_SCOPE_PARTS):
        request.getfixturevalue("_mock_kr_market_session_calendar")
    if any(part in path for part in _AUTH_DB_SCOPE_PARTS):
        request.getfixturevalue("mock_auth_middleware_db")
    if uses_shared_test_database() and (
        "alpaca_paper" in path
        or "paper_approval_packet" in path
        or "test_trade_retrospective_pending" in path
    ):
        request.getfixturevalue("_serialize_alpaca_paper_db_suites")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _bootstrap_test_schema(request: pytest.FixtureRequest):
    """Apply the test schema once, before the first database-backed test.

    ROB-723's advisory-lock + sentinel DDL barrier remains intact. The fixture
    is now an explicit dependency of database fixtures, so pure unit/static
    tests do not connect to PostgreSQL. Under xdist each worker first creates
    its own run-owned database, which also isolates test rows and broad cleanup.
    """
    selected_items = request.session.items
    requires_database = any(
        item.get_closest_marker("integration")
        or _DATABASE_FIXTURE_NAMES.intersection(item.fixturenames)
        for item in selected_items
    )
    if not requires_database:
        # Pure unit/static selections are a DB-free contract. Legacy direct-DB
        # files remain covered by their integration marker, while explicit DB
        # fixtures also force this barrier even if a marker is accidentally
        # omitted.
        yield
        return

    database_created = False
    database_seconds = 0.0
    engine = None
    try:
        database_started = time.perf_counter()
        database_created = await ensure_run_owned_database()
        database_seconds = time.perf_counter() - database_started

        from sqlalchemy import text

        from app.core.db import engine as app_engine
        from tests._db_retry import run_with_deadlock_retry
        from tests._investment_reports_helpers import INVESTMENT_REPORTS_TEST_LOCK_ID
        from tests._schema_bootstrap import apply_test_schema, schema_content_hash

        engine = app_engine
        wanted = schema_content_hash()
        schema_applied = False

        async def _bootstrap_once() -> None:
            nonlocal schema_applied
            async with app_engine.connect() as guard:
                await guard.execute(
                    text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )
                try:
                    async with app_engine.begin() as conn:
                        await conn.execute(
                            text(
                                "CREATE TABLE IF NOT EXISTS "
                                "public._pytest_schema_ready ("
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
                        schema_applied = True
                        await conn.execute(
                            text("DELETE FROM public._pytest_schema_ready")
                        )
                        await conn.execute(
                            text(
                                "INSERT INTO public._pytest_schema_ready "
                                "(content_hash) VALUES (:h)"
                            ),
                            {"h": wanted},
                        )
                finally:
                    await guard.execute(
                        text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                        {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                    )

        schema_started = time.perf_counter()
        await run_with_deadlock_retry(_bootstrap_once)
        request.config.stash[_SCHEMA_METRICS_KEY].append(
            {
                "database": os.environ.get(_XDIST_DATABASE_NAME_ENV, "test_db"),
                "database_created": database_created,
                "database_seconds": database_seconds,
                "schema_applied": schema_applied,
                "schema_seconds": time.perf_counter() - schema_started,
            }
        )
        yield
    finally:
        if uses_run_owned_database():
            try:
                if engine is not None:
                    await engine.dispose()
            finally:
                await drop_run_owned_database()


# Database fixtures for integration tests


@pytest_asyncio.fixture
async def db_session(_bootstrap_test_schema):
    """Async session against the current test database.

    Schema is owned by the explicit session-scoped bootstrap dependency; this
    fixture performs no DDL. Xdist workers use isolated databases.
    """
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def investment_reports_cleanup_lock(db_session):
    """Isolate committed investment-report rows for global ``db_session`` tests.

    Most investment-report table tests use
    ``tests._investment_reports_helpers.session``. That fixture uses an outer
    transaction plus SAVEPOINT-aware sessions, so ordinary test cleanup is a
    rollback rather than committed table-wide deletion.

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

    async def _delete_investment_report_rows() -> None:
        # Keep this cleanup aligned with tests._investment_reports_helpers.session:
        # only the investment-report table family is reset here. Snapshot/stage
        # tables use UUID-scoped test rows and are intentionally left alone.
        async with engine.begin() as cleanup:
            for table in reversed(INVESTMENT_REPORTS_TABLES):
                table_name = table.name  # type: ignore[attr-defined]
                await cleanup.execute(text(f'DELETE FROM review."{table_name}"'))

    guard = None
    try:
        if uses_shared_test_database():
            guard = await engine.connect()
            await guard.execute(
                text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
            )
        await db_session.rollback()
        await run_with_deadlock_retry(
            _delete_investment_report_rows,
            rollback=db_session.rollback,
        )
        try:
            yield db_session
        finally:
            await db_session.rollback()
            await run_with_deadlock_retry(
                _delete_investment_report_rows,
                rollback=db_session.rollback,
            )
    finally:
        if guard is not None:
            try:
                await guard.execute(
                    text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                    {"lock_id": INVESTMENT_REPORTS_TEST_LOCK_ID},
                )
            finally:
                await guard.close()


@pytest_asyncio.fixture
async def retrospective_action_control_lock(_bootstrap_test_schema):
    """Serialize tests that mutate the global retrospective-action authority.

    The ROB-878/880 migration and cutover contracts intentionally change the
    singleton control row and, in a few cases, rebuild its tables.  Xdist
    workers share one PostgreSQL database, so those tests must not observe one
    another's temporary canonical mode or DDL state.
    """
    if uses_run_owned_database():
        yield
        return

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
async def toss_ledger_cleanup_lock(_bootstrap_test_schema):
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
async def binance_demo_reservation_lock(_bootstrap_test_schema):
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
