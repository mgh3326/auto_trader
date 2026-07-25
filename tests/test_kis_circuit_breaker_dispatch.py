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
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 0  # 1 attempt — fail fast per call in tests
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0
    # breaker knobs (read via the injected settings_obj on the breaker, below)


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _FakeSettings()


class _BreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 3
    kis_circuit_breaker_cooldown_seconds = 45


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture(autouse=True)
def _install_breaker(clock):
    # Inject a deterministic-clock breaker as THE process singleton.
    cb._breaker = KISCircuitBreaker(now=clock.now, settings_obj=_BreakerSettings())
    yield
    cb.reset_kis_circuit_breaker()


def _client_with(execute):
    client = _FakeClient()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    client._get_limiter = AsyncMock(return_value=limiter)  # type: ignore[method-assign]
    client._ensure_client = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    client._execute_http_request = execute  # type: ignore[method-assign]
    return client, limiter


async def _call(client):
    return await client._request_with_rate_limit_with_headers(
        "GET", "https://host/uapi/x", headers={}, api_name="inquire_price"
    )


@pytest.mark.asyncio
async def test_connect_failures_open_the_breaker():
    execute = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client, _ = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    assert cb.get_kis_circuit_breaker().state == "open"


@pytest.mark.asyncio
async def test_open_breaker_zero_http_zero_wait():
    execute = AsyncMock(side_effect=httpx.ConnectError(""))
    client, limiter = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await _call(client)
    assert cb.get_kis_circuit_breaker().state == "open"
    execute.reset_mock()
    limiter.acquire.reset_mock()
    with pytest.raises(KISCircuitOpen):
        await _call(client)
    execute.assert_not_awaited()  # ZERO HTTP
    limiter.acquire.assert_not_awaited()  # ZERO rate-limit wait


@pytest.mark.asyncio
async def test_429_response_does_not_open_breaker():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "초과"}
    execute = AsyncMock(return_value=resp)
    client, _ = _client_with(execute)
    for _ in range(6):
        await _call(client)  # KIS-reachable throttle body, returned not raised
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_success_returns_and_keeps_closed():
    ok = MagicMock()
    ok.status_code = 200
    ok.headers = {}
    ok.json.return_value = {"rt_cd": "0", "output": []}
    client, _ = _client_with(AsyncMock(return_value=ok))
    data, _headers = await _call(client)
    assert data["rt_cd"] == "0"
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_cooldown_half_open_probe_closes_on_success(clock):
    ok = MagicMock()
    ok.status_code = 200
    ok.headers = {}
    ok.json.return_value = {"rt_cd": "0", "output": []}
    execute = AsyncMock(side_effect=[httpx.ConnectTimeout("")] * 3 + [ok])
    client, _ = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    assert cb.get_kis_circuit_breaker().state == "open"
    clock.advance(45)
    data, _headers = await _call(client)  # the single probe -> success -> closed
    assert data["rt_cd"] == "0"
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_disabled_flag_is_complete_passthrough():
    class _Disabled(_BreakerSettings):
        kis_circuit_breaker_enabled = False

    cb._breaker = KISCircuitBreaker(settings_obj=_Disabled())
    execute = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client, limiter = _client_with(execute)
    for _ in range(10):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    # never opens; every call still reached the dispatch (limiter acquired)
    assert cb.get_kis_circuit_breaker().state == "closed"
    assert limiter.acquire.await_count == 10


@pytest.mark.asyncio
async def test_open_stampede_does_not_close_circuit(clock):
    # Locks the "before_request() outside the try/except" invariant: once a
    # half-open probe is in flight, a concurrent caller must raise KISCircuitOpen
    # WITHOUT that raise being reclassified as a reachable error (which would
    # wrongly close the still-probing circuit).
    execute = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client, _ = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    breaker = cb.get_kis_circuit_breaker()
    assert breaker.state == "open"
    clock.advance(45)
    breaker.before_request()  # hands out THE probe -> half_open, in flight
    assert breaker.state == "half_open"
    with pytest.raises(KISCircuitOpen):
        await _call(client)  # stampede caller: must fail-fast
    assert breaker.state == "half_open"  # still probing, NOT closed
