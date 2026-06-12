from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from pydantic import SecretStr
from redis.asyncio import RedisError

from app.services.brokers.toss import auth
from app.services.brokers.toss.errors import (
    TossMissingCredentials,
    TossTokenIssuanceUnavailable,
)

pytestmark = pytest.mark.asyncio


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, bool]] = []

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        del ex
        self.set_calls.append((key, value, nx))
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.strings:
                self.strings.pop(key, None)
                removed += 1
        return removed

    async def eval(self, script: str, key_count: int, key: str, token: str) -> int:
        del script, key_count
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0


@dataclass
class _Settings:
    toss_api_enabled: bool = True
    toss_api_client_id: str | None = "client-id"
    toss_api_client_secret: SecretStr | None = SecretStr("client-secret")
    toss_api_base_url: str | None = "https://openapi.tossinvest.com"


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _get_client():
        return redis

    monkeypatch.setattr(auth, "_get_redis_client", _get_client)
    monkeypatch.setattr(auth, "TOKEN_WAIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(auth, "TOKEN_WAIT_POLL_SECONDS", 0.01)
    return redis


async def test_from_settings_fails_closed_missing_gate() -> None:
    settings = _Settings(toss_api_enabled=False)

    with pytest.raises(TossMissingCredentials) as exc_info:
        auth.TossOAuthTokenManager.from_settings(settings)

    assert "TOSS_API_ENABLED" in str(exc_info.value)
    assert "client-secret" not in str(exc_info.value)


async def test_concurrent_cold_start_issues_exactly_once(fake_redis, monkeypatch):
    issue_calls = 0
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        await asyncio.sleep(0.05)
        return auth.TossToken(access_token="issued-token", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    results = await asyncio.gather(*(manager.get_access_token() for _ in range(10)))

    assert results == ["issued-token"] * 10
    assert issue_calls == 1


async def test_contender_times_out_without_independent_issue(fake_redis, monkeypatch):
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    fake_redis.strings[manager.lock_key] = "other-owner"
    issue_calls = 0

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        return auth.TossToken(access_token="must-not-issue", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    with pytest.raises(TossTokenIssuanceUnavailable):
        await manager.get_access_token()

    assert issue_calls == 0


async def test_dead_port_redis_is_not_masked(monkeypatch):
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )

    async def _broken_client():
        raise RedisError("Connection refused on dead test port")

    monkeypatch.setattr(auth, "_get_redis_client", _broken_client)

    with pytest.raises(RedisError, match="dead test port"):
        await manager.get_access_token()


async def test_cached_token_is_reused(fake_redis):
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    fake_redis.strings[manager.token_key] = json.dumps(
        {"access_token": "cached-token", "expires_at": 4_102_444_800.0}
    )

    assert await manager.get_access_token() == "cached-token"
