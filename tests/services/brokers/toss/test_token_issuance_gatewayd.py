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


def _manager() -> auth.TossOAuthTokenManager:
    return auth.TossOAuthTokenManager(
        client_id="gatewayd-client-id",
        client_secret=SecretStr("gatewayd-client-secret"),
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
        fake_redis.strings[manager.token_key] = json.dumps(
            {"access_token": "toss-gatewayd-token", "expires_at": time.time() + 3600}
        )

    ensure = AsyncMock(side_effect=ensure_gatewayd)
    monkeypatch.setattr(auth, "ensure_gatewayd_token", ensure)

    assert await manager.get_access_token() == "toss-gatewayd-token"

    ensure.assert_awaited_once_with("toss", settings_obj=settings)
    oauth.assert_not_awaited()


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
