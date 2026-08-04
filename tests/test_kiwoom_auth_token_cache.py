# tests/test_kiwoom_auth_token_cache.py
"""Verify Kiwoom OAuth token cache: refresh on expiry, never log secret/token."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
import time

import httpx
import pytest

from app.services.brokers.kiwoom import auth as auth_module
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.auth import (
    KiwoomAuthClient,
    KiwoomRedisConfigurationError,
    KiwoomToken,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        del ex
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.strings:
                self.strings.pop(key)
                removed += 1
        return removed

    async def eval(
        self,
        script: str,
        key_count: int,
        key: str,
        lock_token: str,
    ) -> int:
        del script, key_count
        if self.strings.get(key) == lock_token:
            self.strings.pop(key)
            return 1
        return 0


@pytest.mark.asyncio
async def test_scoped_redis_env_does_not_import_global_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRedis()
    captured: dict[str, object] = {}

    def from_url(url: str, **kwargs: object) -> _FakeRedis:
        captured["url_present"] = bool(url)
        captured.update(kwargs)
        return fake

    monkeypatch.setenv("REDIS_URL", "redis://scoped.invalid:6379/0")
    monkeypatch.delenv("REDIS_MAX_CONNECTIONS", raising=False)
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT", raising=False)
    monkeypatch.delenv("REDIS_SOCKET_CONNECT_TIMEOUT", raising=False)
    monkeypatch.setattr(auth_module.redis, "from_url", from_url)
    monkeypatch.setattr(auth_module, "_redis_client", None)
    sys.modules.pop("app.core.config", None)

    client = await auth_module._get_redis_client()

    assert client is fake
    assert captured == {
        "url_present": True,
        "max_connections": 20,
        "socket_timeout": 5.0,
        "socket_connect_timeout": 5.0,
        "decode_responses": True,
    }
    assert "app.core.config" not in sys.modules


def test_scoped_redis_env_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(KiwoomRedisConfigurationError, match="REDIS_URL"):
        auth_module._redis_client_from_env()


@pytest.fixture
def fake_redis():
    return _FakeRedis()


@pytest.fixture
def mock_token_transport() -> httpx.MockTransport:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert request.method == "POST"
        assert request.url.path == constants.OAUTH_PATH
        body = request.read()
        # Body must NOT echo as raw secret in log; presence is fine.
        assert b"client_credentials" in body
        return httpx.Response(
            200,
            json={
                "token": f"tkn-{calls['count']}",
                "expires_dt": (
                    dt.datetime.now(dt.UTC) + dt.timedelta(seconds=300)
                ).strftime("%Y%m%d%H%M%S"),
                "token_type": "Bearer",
                "return_code": 0,
                "return_msg": "정상",
            },
        )

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


@pytest.mark.asyncio
async def test_token_is_cached_until_near_expiry(mock_token_transport, fake_redis):
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
        redis_client=fake_redis,
    )
    t1 = await auth.get_token()
    t2 = await auth.get_token()
    assert t1 == t2
    assert mock_token_transport.calls["count"] == 1


@pytest.mark.asyncio
async def test_concurrent_token_requests_share_one_refresh(
    mock_token_transport, fake_redis
):
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
        redis_client=fake_redis,
    )

    tokens = await asyncio.gather(*(auth.get_token() for _ in range(12)))

    assert tokens == ["tkn-1"] * 12
    assert mock_token_transport.calls["count"] == 1


@pytest.mark.asyncio
async def test_token_refreshed_when_expired(mock_token_transport, fake_redis):
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
        redis_client=fake_redis,
    )
    await auth.get_token()
    raw = await fake_redis.get(auth.token_key)
    cached = json.loads(raw)
    cached["expires_at"] = time.time() - 1
    await fake_redis.set(auth.token_key, json.dumps(cached))
    await auth.get_token()
    assert mock_token_transport.calls["count"] == 2


@pytest.mark.asyncio
async def test_logs_never_contain_secret_or_token(
    caplog, mock_token_transport, fake_redis
):
    caplog.set_level(logging.DEBUG, logger="app.services.brokers.kiwoom")
    auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL,
        app_key="ak",
        app_secret="SECRET-VAL",
        transport=mock_token_transport,
        redis_client=fake_redis,
    )
    token = await auth.get_token()
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "SECRET-VAL" not in rendered
    assert token not in rendered


@pytest.mark.asyncio
async def test_concurrent_managers_share_one_redis_single_flight(
    monkeypatch, fake_redis
):
    managers = [
        KiwoomAuthClient(
            base_url=constants.MOCK_BASE_URL,
            app_key="shared-app-key",
            app_secret="SECRET-VAL",
            redis_client=fake_redis,
        )
        for _ in range(8)
    ]
    issue_calls = 0

    async def issue_token() -> KiwoomToken:
        nonlocal issue_calls
        issue_calls += 1
        await asyncio.sleep(0.05)
        return KiwoomToken(
            access_token="shared-token",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        )

    for manager in managers:
        monkeypatch.setattr(manager, "_issue_token", issue_token)

    tokens = await asyncio.gather(*(manager.get_token() for manager in managers))

    assert tokens == ["shared-token"] * len(managers)
    assert issue_calls == 1


@pytest.mark.asyncio
async def test_concurrent_failed_token_reissue_mints_once(monkeypatch, fake_redis):
    managers = [
        KiwoomAuthClient(
            base_url=constants.MOCK_BASE_URL,
            app_key="shared-app-key",
            app_secret="SECRET-VAL",
            redis_client=fake_redis,
        )
        for _ in range(8)
    ]
    stale = KiwoomToken(
        access_token="stale-token",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
    )
    await managers[0]._cache_token(stale)
    issue_calls = 0

    async def issue_token() -> KiwoomToken:
        nonlocal issue_calls
        issue_calls += 1
        await asyncio.sleep(0.05)
        return KiwoomToken(
            access_token="fresh-token",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        )

    for manager in managers:
        monkeypatch.setattr(manager, "_issue_token", issue_token)

    tokens = await asyncio.gather(
        *(
            manager.get_token(
                force_reissue=True,
                failed_token="stale-token",
            )
            for manager in managers
        )
    )

    assert tokens == ["fresh-token"] * len(managers)
    assert issue_calls == 1
