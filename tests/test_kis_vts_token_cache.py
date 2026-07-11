from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.services.brokers.kis.client import KISClient

pytestmark = pytest.mark.unit


class _FakeRedis:
    """Minimal stateful Redis double for token cache and lock behavior."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def execute_command(self, *args: Any) -> int:
        command, _script, key_count, key, lock_value = args
        assert command == "EVAL"
        assert key_count == 1
        if self.values.get(key) != lock_value:
            return 0
        del self.values[key]
        return 1


def _expected_mock_namespace(base_url: str, app_key: str) -> str:
    host = base_url.removeprefix("https://").removeprefix("http://").rstrip("/")
    fingerprint = hashlib.sha256(app_key.encode()).hexdigest()[:16]
    return f"kis_mock:{host.lower()}:{fingerprint}"


def _set_mock_credentials(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str,
    app_key: str,
) -> None:
    monkeypatch.setattr(settings, "kis_mock_base_url", base_url)
    monkeypatch.setattr(settings, "kis_mock_app_key", app_key)
    monkeypatch.setattr(settings, "kis_mock_app_secret", "test-vts-secret")
    monkeypatch.setattr(settings, "kis_mock_access_token", None)


def _token_response(token: str, *, expires_in: int = 7200) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "access_token": token,
        "expires_in": expires_in,
    }
    return response


@pytest.mark.asyncio
async def test_vts_cache_hit_skips_token_post(monkeypatch: pytest.MonkeyPatch) -> None:
    base_url = "https://vts-cache-hit.example:29443"
    app_key = "vts-cache-hit-app-key"
    _set_mock_credentials(monkeypatch, base_url=base_url, app_key=app_key)

    client = KISClient(is_mock=True)
    expected_namespace = _expected_mock_namespace(base_url, app_key)
    expected_key = f"{expected_namespace}:access_token"
    assert client._token_manager._token_key == expected_key

    fake_redis = _FakeRedis()
    fake_redis.values[expected_key] = json.dumps(
        {
            "access_token": "cached-vts-token",
            "expires_at": time.time() + 7200,
            "created_at": time.time(),
        }
    )
    client._token_manager.redis_client = fake_redis  # type: ignore[assignment]
    http_client = AsyncMock()
    ensure_client = AsyncMock(return_value=http_client)
    monkeypatch.setattr(client, "_ensure_client", ensure_client)

    await client._ensure_token()

    assert settings.kis_mock_access_token == "cached-vts-token"
    ensure_client.assert_not_awaited()
    http_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_vts_token_reissues_once_and_replaces_scoped_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = "https://vts-expired.example:29443"
    app_key = "vts-expired-app-key"
    _set_mock_credentials(monkeypatch, base_url=base_url, app_key=app_key)

    client = KISClient(is_mock=True)
    expected_namespace = _expected_mock_namespace(base_url, app_key)
    expected_key = f"{expected_namespace}:access_token"
    assert client._token_manager._token_key == expected_key

    fake_redis = _FakeRedis()
    fake_redis.values[expected_key] = json.dumps(
        {
            "access_token": "expired-vts-token",
            "expires_at": time.time() - 1,
            "created_at": time.time() - 7200,
        }
    )
    client._token_manager.redis_client = fake_redis  # type: ignore[assignment]
    http_client = AsyncMock()
    http_client.post.return_value = _token_response("fresh-vts-token")
    monkeypatch.setattr(
        client,
        "_ensure_client",
        AsyncMock(return_value=http_client),
    )

    await client._ensure_token()

    http_client.post.assert_awaited_once()
    assert http_client.post.await_args.args[0] == f"{base_url}/oauth2/token"
    assert json.loads(fake_redis.values[expected_key])["access_token"] == (
        "fresh-vts-token"
    )
    assert "kis_mock:access_token" not in fake_redis.values


def test_vts_manager_is_shared_and_keys_are_scoped_by_host_and_appkey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.invest_home_readers import SafeKISMockClient
    from app.services.redis_token_manager import redis_token_manager

    live = KISClient()

    host_a = "https://vts-scope-a.example:29443"
    app_key_a = "vts-scope-app-key-a"
    _set_mock_credentials(monkeypatch, base_url=host_a, app_key=app_key_a)
    mock_a = KISClient(is_mock=True)
    mock_a_again = KISClient(is_mock=True)
    safe_mock_a = SafeKISMockClient()
    _set_mock_credentials(
        monkeypatch,
        base_url=f"{host_a}/",
        app_key=app_key_a,
    )
    mock_a_trailing_slash = KISClient(is_mock=True)

    app_key_b = "vts-scope-app-key-b"
    _set_mock_credentials(monkeypatch, base_url=host_a, app_key=app_key_b)
    mock_b = KISClient(is_mock=True)

    host_c = "https://vts-scope-c.example:29443"
    _set_mock_credentials(monkeypatch, base_url=host_c, app_key=app_key_a)
    mock_c = KISClient(is_mock=True)

    assert live._token_manager._token_key == "kis:access_token"
    assert live._token_manager._lock_key == "kis:token:lock"
    assert live._token_manager is redis_token_manager
    assert live._token_manager._lock_wait_timeout_seconds == 3.0
    assert mock_a._token_manager is mock_a_again._token_manager
    assert mock_a._token_manager is safe_mock_a._token_manager
    assert mock_a._token_manager is mock_a_trailing_slash._token_manager
    assert mock_a._token_manager._lock_wait_timeout_seconds == 11.0
    assert safe_mock_a._token_request_timeout() == 10.0

    token_keys = {
        live._token_manager._token_key,
        mock_a._token_manager._token_key,
        mock_b._token_manager._token_key,
        mock_c._token_manager._token_key,
    }
    lock_keys = {
        live._token_manager._lock_key,
        mock_a._token_manager._lock_key,
        mock_b._token_manager._lock_key,
        mock_c._token_manager._lock_key,
    }
    assert len(token_keys) == 4
    assert len(lock_keys) == 4
    assert all(app_key_a not in key and app_key_b not in key for key in token_keys)
    assert all(app_key_a not in key and app_key_b not in key for key in lock_keys)


def test_vts_namespace_normalizes_host_without_exposing_empty_appkey() -> None:
    from app.services.redis_token_manager import _kis_mock_token_namespace

    expected_empty_fingerprint = hashlib.sha256(b"").hexdigest()[:16]

    assert (
        _kis_mock_token_namespace(
            base_url="HTTPS://VTS-NAMESPACE.EXAMPLE:29443/oauth2/token?ignored=yes",
            app_key="",
        )
        == f"kis_mock:vts-namespace.example:29443:{expected_empty_fingerprint}"
    )
    with pytest.raises(ValueError, match="must include a host"):
        _kis_mock_token_namespace(base_url="not-a-url", app_key="test")


@pytest.mark.asyncio
async def test_same_scope_vts_cold_start_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = "https://vts-single-flight.example:29443"
    app_key = "vts-single-flight-app-key"
    _set_mock_credentials(monkeypatch, base_url=base_url, app_key=app_key)
    first = KISClient(is_mock=True)

    # Model a second worker process: it has a different manager instance but
    # shares the same Redis namespace and backing Redis service.
    from app.services.redis_token_manager import _get_kis_mock_token_manager

    _get_kis_mock_token_manager.cache_clear()
    second = KISClient(is_mock=True)
    assert first._token_manager is not second._token_manager
    assert first._token_manager._token_key == second._token_manager._token_key

    fake_redis = _FakeRedis()
    first._token_manager.redis_client = fake_redis  # type: ignore[assignment]
    second._token_manager.redis_client = fake_redis  # type: ignore[assignment]

    fetch_started = asyncio.Event()

    async def slow_vts_token_post(*args: Any, **kwargs: Any) -> MagicMock:
        del args, kwargs
        fetch_started.set()
        # Longer than the legacy distributed-lock waiter budget (~3.2s), but
        # within the VTS OAuth request budget (10s).
        await asyncio.sleep(3.4)
        return _token_response("single-flight-vts-token")

    first_http = AsyncMock()
    first_http.post.side_effect = slow_vts_token_post
    second_http = AsyncMock()
    second_http.post.return_value = _token_response("single-flight-vts-token")
    monkeypatch.setattr(
        first,
        "_ensure_client",
        AsyncMock(return_value=first_http),
    )
    monkeypatch.setattr(
        second,
        "_ensure_client",
        AsyncMock(return_value=second_http),
    )

    first_task = asyncio.create_task(first._ensure_token())
    await fetch_started.wait()
    second_task = asyncio.create_task(second._ensure_token())
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result is None
    assert second_result is None
    assert first_http.post.await_count + second_http.post.await_count == 1
    assert settings.kis_mock_access_token == "single-flight-vts-token"
    cached = json.loads(fake_redis.values[first._token_manager._token_key])
    assert cached["access_token"] == "single-flight-vts-token"


@pytest.mark.asyncio
async def test_vts_token_timeout_is_longer_while_live_stays_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mock_credentials(
        monkeypatch,
        base_url="https://vts-timeout.example:29443",
        app_key="vts-timeout-app-key",
    )
    mock_client = KISClient(is_mock=True)
    mock_http = AsyncMock()
    mock_http.post.return_value = _token_response("mock-timeout-token")
    mock_ensure_client = AsyncMock(return_value=mock_http)
    monkeypatch.setattr(mock_client, "_ensure_client", mock_ensure_client)

    await mock_client._fetch_token()

    mock_ensure_client.assert_awaited_once_with(timeout=10.0)
    assert mock_http.post.await_args.kwargs["timeout"] == 10.0

    live_client = KISClient()
    live_http = AsyncMock()
    live_http.post.return_value = _token_response("live-timeout-token")
    live_ensure_client = AsyncMock(return_value=live_http)
    monkeypatch.setattr(live_client, "_ensure_client", live_ensure_client)

    await live_client._fetch_token()

    live_ensure_client.assert_awaited_once_with(timeout=5.0)
    assert live_http.post.await_args.kwargs["timeout"] == 5.0
