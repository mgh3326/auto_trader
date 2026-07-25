from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis import circuit_breaker as cb
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.circuit_breaker import KISCircuitBreaker, KISCircuitOpen
from app.services.redis_token_manager import RedisTokenManager

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


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


class _EnabledBreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 1  # 1 failure -> open
    kis_circuit_breaker_cooldown_seconds = 45


@pytest.fixture(autouse=True)
def _install_breaker():
    cb._breaker = KISCircuitBreaker(settings_obj=_EnabledBreakerSettings())
    yield
    cb.reset_kis_circuit_breaker()


async def test_refresh_token_with_lock_reraises_circuit_open_and_releases_lock(
    monkeypatch,
):
    # The token single-flight must NOT swallow KISCircuitOpen and must still
    # release the distributed lock. Stub the cache/lock probes so the real
    # try/finally body runs with no real sleep / no real Redis.
    monkeypatch.setattr("app.services.redis_token_manager.asyncio.sleep", AsyncMock())
    manager = RedisTokenManager()
    manager.get_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
    manager._acquire_lock = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._release_lock = AsyncMock()  # type: ignore[method-assign]
    manager.save_token = AsyncMock()  # type: ignore[method-assign]

    fetcher = AsyncMock(side_effect=KISCircuitOpen(45.0))

    with pytest.raises(KISCircuitOpen):
        await manager.refresh_token_with_lock(fetcher)

    fetcher.assert_awaited_once()  # single-flight: exactly one fetch attempt
    manager._release_lock.assert_awaited()  # lock released on the error path
    manager.save_token.assert_not_awaited()  # no token persisted on failure


async def test_open_breaker_ensure_token_fails_fast_zero_http(monkeypatch):
    # End-to-end: open breaker -> _ensure_token (cache miss) -> single-flight ->
    # _fetch_token.before_request() -> KISCircuitOpen, with ZERO token POST.
    monkeypatch.setattr("app.services.redis_token_manager.asyncio.sleep", AsyncMock())
    breaker = cb.get_kis_circuit_breaker()
    breaker.record_failure()  # threshold=1 -> OPEN
    assert breaker.state == "open"

    manager = RedisTokenManager()
    manager.get_token = AsyncMock(return_value=None)  # cache miss everywhere
    manager._acquire_lock = AsyncMock(return_value=True)
    manager._release_lock = AsyncMock()
    manager.save_token = AsyncMock()

    client = _FakeClient()
    client._token_manager = manager
    post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    fake_http = MagicMock()
    fake_http.post = post
    client._ensure_client = AsyncMock(return_value=fake_http)  # type: ignore[method-assign]

    with pytest.raises(KISCircuitOpen):
        await client._ensure_token()

    post.assert_not_awaited()  # ZERO token HTTP — failed fast at before_request
    client._ensure_client.assert_not_awaited()
    manager._release_lock.assert_awaited()  # single-flight lock still released
