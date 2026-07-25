from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis import circuit_breaker as cb
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.circuit_breaker import KISCircuitBreaker, KISCircuitOpen

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeSettings:
    def __init__(self) -> None:
        self.kis_app_key = "key"
        self.kis_app_secret = "secret"
        self.kis_access_token = "token"
        self.kis_base_url = "https://openapi.koreainvestment.com:9443"


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        type(self)._shared_client_lock = None
        self._fake_settings = _FakeSettings()

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return self._fake_settings


class _BreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 3
    kis_circuit_breaker_cooldown_seconds = 45


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture(autouse=True)
def _install_breaker(clock):
    # Inject a deterministic-clock, ENABLED breaker as THE process singleton.
    # (conftest._isolate_kis_circuit_breaker disables the GLOBAL settings flag,
    # but this breaker reads its own settings_obj, so it stays enabled here.)
    cb._breaker = KISCircuitBreaker(now=clock.now, settings_obj=_BreakerSettings())
    yield
    cb.reset_kis_circuit_breaker()


def _client_with_post(post):
    client = _FakeClient()
    fake_http = MagicMock()
    fake_http.post = post
    client._ensure_client = AsyncMock(return_value=fake_http)  # type: ignore[method-assign]
    return client


def _json_response(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout(""),
        httpx.ConnectError(""),
        httpx.PoolTimeout(""),
        httpx.ReadTimeout(""),
        ConnectionRefusedError(),
    ],
)
async def test_token_connect_failures_open_breaker(exc):
    post = AsyncMock(side_effect=exc)
    client = _client_with_post(post)
    for _ in range(3):  # threshold
        with pytest.raises(type(exc)):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "open"


@pytest.mark.asyncio
async def test_open_breaker_token_fetch_zero_http():
    post = AsyncMock(side_effect=httpx.ConnectError(""))
    client = _client_with_post(post)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "open"
    post.reset_mock()
    client._ensure_client.reset_mock()
    with pytest.raises(KISCircuitOpen):
        await client._fetch_token()
    post.assert_not_awaited()  # ZERO HTTP
    client._ensure_client.assert_not_awaited()  # not even the client build


@pytest.mark.asyncio
async def test_token_401_invalid_key_does_not_open():
    # KIS responded with an error body lacking access_token -> KeyError (reachable).
    post = AsyncMock(return_value=_json_response({"error": "invalid_client"}))
    client = _client_with_post(post)
    for _ in range(6):  # well past threshold
        with pytest.raises(KeyError):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "closed"
    assert cb.get_kis_circuit_breaker().failure_count == 0


@pytest.mark.asyncio
async def test_token_non_json_body_does_not_open():
    r = MagicMock()
    r.json.side_effect = ValueError("not json")
    post = AsyncMock(return_value=r)
    client = _client_with_post(post)
    for _ in range(6):
        with pytest.raises(ValueError):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_token_success_returns_and_keeps_closed():
    post = AsyncMock(
        return_value=_json_response({"access_token": "T", "expires_in": 100})
    )
    client = _client_with_post(post)
    token, expires = await client._fetch_token()
    assert token == "T"
    assert expires == 100
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_token_success_after_failures_resets_count():
    post = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout(""),
            httpx.ConnectTimeout(""),
            _json_response({"access_token": "T", "expires_in": 100}),
        ]
    )
    client = _client_with_post(post)
    for _ in range(2):
        with pytest.raises(httpx.ConnectTimeout):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().failure_count == 2
    await client._fetch_token()  # success resets the counter
    assert cb.get_kis_circuit_breaker().failure_count == 0
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_disabled_flag_token_passthrough():
    class _Disabled(_BreakerSettings):
        kis_circuit_breaker_enabled = False

    cb._breaker = KISCircuitBreaker(settings_obj=_Disabled())
    post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client = _client_with_post(post)
    for _ in range(10):
        with pytest.raises(httpx.ConnectTimeout):
            await client._fetch_token()
    # never opens; every call still reached the network (passthrough)
    assert cb.get_kis_circuit_breaker().state == "closed"
    assert post.await_count == 10


@pytest.mark.asyncio
async def test_half_open_probe_stampede_does_not_close(clock):
    # Locks "before_request() OUTSIDE the classify try/except": once a half-open
    # probe is in flight, a stampede caller must raise KISCircuitOpen WITHOUT
    # that raise being reclassified reachable (which would wrongly close the
    # still-probing circuit).
    post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client = _client_with_post(post)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await client._fetch_token()
    breaker = cb.get_kis_circuit_breaker()
    assert breaker.state == "open"
    clock.advance(45)
    breaker.before_request()  # hands out THE probe -> half_open, in flight
    assert breaker.state == "half_open"
    with pytest.raises(KISCircuitOpen):
        await client._fetch_token()  # stampede caller: must fail-fast
    assert breaker.state == "half_open"  # still probing, NOT closed


@pytest.mark.asyncio
async def test_cached_token_path_no_breaker_involvement():
    # _ensure_token cache-hit must be byte-identical: _fetch_token never called,
    # breaker never touched (a pre-loaded failure count is preserved).
    breaker = cb.get_kis_circuit_breaker()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.failure_count == 2

    client = _FakeClient()
    client._token_manager = MagicMock()
    client._token_manager.get_token = AsyncMock(return_value="cached-tok")
    client._fetch_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("cache hit must not fetch")
    )

    await client._ensure_token()
    assert client._settings.kis_access_token == "cached-tok"
    client._fetch_token.assert_not_awaited()
    assert breaker.failure_count == 2  # untouched
    assert breaker.state == "closed"
