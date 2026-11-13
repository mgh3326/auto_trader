from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import RedisError

from app.services.brokers.toss import auth
from app.services.brokers.toss.errors import (
    TossApiDisabled,
    TossMissingCredentials,
    TossTokenIssuanceUnavailable,
)
from app.services.brokers.toss.rate_limiter import TossApiGroup

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
    broker_token_issuance_mode: str = "self"
    toss_token_namespace_fp: str | None = None


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _get_client():
        return redis

    monkeypatch.setattr(auth, "_get_redis_client", _get_client)
    monkeypatch.setattr(auth, "TOKEN_WAIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(auth, "TOKEN_WAIT_POLL_SECONDS", 0.01)
    return redis


async def test_from_settings_disabled_raises_toss_api_disabled() -> None:
    settings = _Settings(toss_api_enabled=False)

    with pytest.raises(TossApiDisabled) as exc_info:
        auth.TossOAuthTokenManager.from_settings(settings)

    assert "TOSS_API_ENABLED" in str(exc_info.value)
    assert "client-secret" not in str(exc_info.value)


async def test_from_settings_missing_credentials_raises_names_only() -> None:
    settings = _Settings(
        toss_api_enabled=True,
        toss_api_client_id=None,
        toss_api_client_secret=None,
    )

    with pytest.raises(TossMissingCredentials) as exc_info:
        auth.TossOAuthTokenManager.from_settings(settings)

    message = str(exc_info.value)
    assert "TOSS_API_CLIENT_ID" in message
    assert "TOSS_API_CLIENT_SECRET" in message
    assert "client-secret" not in message


@pytest.mark.parametrize("mode", ["gatewayd", "cache_only"])
async def test_consumer_mode_can_build_from_namespace_fingerprint_only(
    mode: str,
) -> None:
    fingerprint = auth.toss_client_fingerprint("client-id")
    expected = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
    )
    configured = _Settings(
        toss_api_enabled=True,
        toss_api_client_id=None,
        toss_api_client_secret=None,
        broker_token_issuance_mode=mode,
        toss_token_namespace_fp=fingerprint,
    )

    manager = auth.TossOAuthTokenManager.from_settings(configured)

    assert (manager.token_key, manager.lock_key) == (
        expected.token_key,
        expected.lock_key,
    )


async def test_namespace_fingerprint_and_client_id_must_match() -> None:
    configured = _Settings(
        broker_token_issuance_mode="gatewayd",
        toss_token_namespace_fp="0123456789abcdef",
    )

    with pytest.raises(TossMissingCredentials) as exc_info:
        auth.TossOAuthTokenManager.from_settings(configured)

    message = str(exc_info.value)
    assert "TOSS_TOKEN_NAMESPACE_FP" in message
    assert "TOSS_API_CLIENT_ID" in message
    assert "0123456789abcdef" not in message
    assert "client-id" not in message


async def test_matching_namespace_fingerprint_and_client_id_are_accepted() -> None:
    configured = _Settings(
        toss_api_client_secret=None,
        broker_token_issuance_mode="cache_only",
        toss_token_namespace_fp=auth.toss_client_fingerprint("client-id"),
    )

    manager = auth.TossOAuthTokenManager.from_settings(configured)

    assert manager.client_fingerprint == auth.toss_client_fingerprint("client-id")


async def test_empty_namespace_fingerprint_is_ignored_in_self_mode() -> None:
    configured = _Settings(toss_token_namespace_fp="")

    manager = auth.TossOAuthTokenManager.from_settings(configured)
    expected = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
    )

    assert (manager.token_key, manager.lock_key) == (
        expected.token_key,
        expected.lock_key,
    )


@pytest.mark.parametrize("fingerprint", ["short", "g123456789abcdef", "0" * 17])
async def test_malformed_namespace_fingerprint_is_refused(fingerprint: str) -> None:
    configured = _Settings(
        broker_token_issuance_mode="gatewayd",
        toss_api_client_id=None,
        toss_api_client_secret=None,
        toss_token_namespace_fp=fingerprint,
    )

    with pytest.raises(TossMissingCredentials) as exc_info:
        auth.TossOAuthTokenManager.from_settings(configured)

    assert "TOSS_TOKEN_NAMESPACE_FP" in str(exc_info.value)
    assert fingerprint not in str(exc_info.value)


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


async def test_force_reissue_returns_fresher_cached_token_without_reissuing(
    fake_redis, monkeypatch
):
    """ROB-547: a contender that hit invalid-token must reuse a fresher cached
    token instead of force-reissuing it away (single-valid-token churn)."""
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    # Cache already holds a fresher token T2 (another process reissued).
    fake_redis.strings[manager.token_key] = json.dumps(
        {"access_token": "fresh-T2", "expires_at": 4_102_444_800.0}
    )
    issue_calls = 0

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        return auth.TossToken(access_token="must-not-issue-T3", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    token = await manager.get_access_token(force_reissue=True, failed_token="stale-T1")

    assert token == "fresh-T2"
    assert issue_calls == 0


async def test_force_reissue_reissues_when_cache_still_holds_failed_token(
    fake_redis, monkeypatch
):
    """ROB-547: if the cache still holds the failed token, force-reissue replaces it."""
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    fake_redis.strings[manager.token_key] = json.dumps(
        {"access_token": "stale-T1", "expires_at": 4_102_444_800.0}
    )
    issue_calls = 0

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        return auth.TossToken(access_token="new-T2", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    token = await manager.get_access_token(force_reissue=True, failed_token="stale-T1")

    assert token == "new-T2"
    assert issue_calls == 1


async def test_contended_force_reissue_never_returns_failed_token(
    fake_redis, monkeypatch
):
    """ROB-547: a contender waiting after invalid-token must not get the dead
    token back; if only the failed token is ever cached, it times out."""
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    fake_redis.strings[manager.lock_key] = "other-owner"
    fake_redis.strings[manager.token_key] = json.dumps(
        {"access_token": "stale-T1", "expires_at": 4_102_444_800.0}
    )
    issue_calls = 0

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        return auth.TossToken(access_token="must-not-issue", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    with pytest.raises(TossTokenIssuanceUnavailable):
        await manager.get_access_token(force_reissue=True, failed_token="stale-T1")

    assert issue_calls == 0


async def test_issue_token_uses_auth_rate_limiter(monkeypatch):
    acquired: list[TossApiGroup] = []

    class FakeLimiter:
        async def acquire(self, group: TossApiGroup) -> None:
            acquired.append(group)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, path, *, data, headers):
            assert path == "/oauth2/token"
            assert data["client_secret"] == "client-secret"
            assert headers["Content-Type"] == "application/x-www-form-urlencoded"
            request = httpx.Request(
                "POST", "https://openapi.tossinvest.com/oauth2/token"
            )
            return httpx.Response(
                200,
                json={"access_token": "issued-token", "expires_in": 86399},
                request=request,
            )

    monkeypatch.setattr(auth, "build_toss_client", lambda *, base_url: FakeClient())

    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
        rate_limiter=FakeLimiter(),
    )

    token = await manager._issue_token()

    assert token.access_token == "issued-token"
    assert acquired == [TossApiGroup.AUTH]
