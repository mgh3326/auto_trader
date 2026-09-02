"""Toss gatewayd token-ownership contracts."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.services.brokers.token_issuance import TokenIssuanceUnavailable
from app.services.brokers.toss import auth
from app.services.brokers.toss.errors import TossLocalTokenIssuanceForbidden


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self, key: str, value: str, nx: bool = False, ex: int | None = None
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

    async def eval(self, *_args: object) -> int:
        key, token = _args[-2:]
        if self.strings.get(str(key)) == token:
            self.strings.pop(str(key))
            return 1
        return 0


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()

    async def get_client() -> _FakeRedis:
        return redis

    monkeypatch.setattr(auth, "_get_redis_client", get_client)
    return redis


def _manager(
    *, client_secret: str = "gatewayd-client-secret"
) -> auth.TossOAuthTokenManager:
    return auth.TossOAuthTokenManager(
        client_id="gatewayd-client-id",
        client_secret=SecretStr(client_secret),
    )


@pytest.mark.asyncio
async def test_gatewayd_miss_ensures_once_reloads_redis_and_never_calls_toss_oauth(
    fake_redis: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    manager = _manager()
    oauth = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", oauth)

    async def ensure_gatewayd(provider: str, *, settings_obj: object) -> None:
        assert provider == "toss"
        assert settings_obj is settings
        if manager.lock_key in fake_redis.strings:
            raise TokenIssuanceUnavailable("gatewayd issuer lock is held by Python")
        fake_redis.strings[manager.token_key] = json.dumps(
            {"access_token": "toss-gatewayd-token", "expires_at": time.time() + 3600}
        )

    ensure = AsyncMock(side_effect=ensure_gatewayd)
    monkeypatch.setattr(auth, "ensure_gatewayd_token", ensure)

    assert await manager.get_access_token() == "toss-gatewayd-token"

    ensure.assert_awaited_once_with("toss", settings_obj=settings)
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_gatewayd_empty_secret_ensures_once_and_reloads_redis(
    fake_redis: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    manager = _manager(client_secret="")
    oauth = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", oauth)

    async def ensure_gatewayd(provider: str, *, settings_obj: object) -> None:
        assert provider == "toss"
        assert settings_obj is settings
        fake_redis.strings[manager.token_key] = json.dumps(
            {
                "access_token": "secretless-gatewayd-token",
                "expires_at": time.time() + 3600,
            }
        )

    ensure = AsyncMock(side_effect=ensure_gatewayd)
    monkeypatch.setattr(auth, "ensure_gatewayd_token", ensure)

    assert await manager.get_access_token() == "secretless-gatewayd-token"

    ensure.assert_awaited_once_with("toss", settings_obj=settings)
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_gatewayd_seals_local_oauth_issuance_even_with_empty_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    manager = _manager(client_secret="")

    with pytest.raises(TossLocalTokenIssuanceForbidden, match="forbidden"):
        await manager._issue_token()


def test_self_mode_rejects_empty_secret_before_local_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "self")

    with pytest.raises(auth.TossMissingCredentials, match="TOSS_API_CLIENT_SECRET"):
        _manager(client_secret="")


@pytest.mark.asyncio
async def test_gatewayd_failure_propagates_without_toss_oauth_fallback(
    fake_redis: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    manager = _manager()
    oauth = AsyncMock(side_effect=AssertionError("Toss OAuth fallback is forbidden"))
    monkeypatch.setattr(manager, "_issue_token", oauth)
    ensure = AsyncMock(side_effect=TokenIssuanceUnavailable("gatewayd unavailable"))
    monkeypatch.setattr(auth, "ensure_gatewayd_token", ensure)

    with pytest.raises(TokenIssuanceUnavailable, match="gatewayd unavailable"):
        await manager.get_access_token()

    ensure.assert_awaited_once_with("toss", settings_obj=settings)
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_gatewayd_force_reissue_keeps_python_read_only_until_gateway_publishes(
    fake_redis: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    manager = _manager()
    failed_token = "known-bad-token"
    fake_redis.strings[manager.token_key] = json.dumps(
        {"access_token": failed_token, "expires_at": time.time() + 3600}
    )

    async def ensure_gatewayd(provider: str, *, settings_obj: object) -> None:
        assert provider == "toss"
        assert settings_obj is settings
        assert fake_redis.strings[manager.token_key]
        assert manager.lock_key not in fake_redis.strings
        fake_redis.strings[manager.token_key] = json.dumps(
            {"access_token": "gatewayd-fresh-token", "expires_at": time.time() + 3600}
        )

    ensure = AsyncMock(side_effect=ensure_gatewayd)
    monkeypatch.setattr(auth, "ensure_gatewayd_token", ensure)
    oauth = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", oauth)

    assert (
        await manager.get_access_token(force_reissue=True, failed_token=failed_token)
        == "gatewayd-fresh-token"
    )

    ensure.assert_awaited_once_with("toss", settings_obj=settings)
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_mode_keeps_existing_toss_oauth_issuer(
    fake_redis: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "self")
    manager = _manager()
    oauth = AsyncMock(return_value=auth.TossToken("self-issued-token", 3600))
    monkeypatch.setattr(manager, "_issue_token", oauth)
    ensure = AsyncMock()
    monkeypatch.setattr(auth, "ensure_gatewayd_token", ensure)

    assert await manager.get_access_token() == "self-issued-token"

    oauth.assert_awaited_once()
    ensure.assert_not_awaited()
