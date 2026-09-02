"""KIS gatewayd token-ownership contracts."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import settings
from app.services.brokers import token_issuance
from app.services.brokers.kis import base as kis_base
from app.services.brokers.kis.client import KISClient
from app.services.brokers.token_issuance import TokenIssuanceUnavailable
from app.services.redis_token_manager import RedisTokenManager


class _FakeRedis:
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
        _command, _script, _key_count, key, lock_value = args
        if self.values.get(key) != lock_value:
            return 0
        del self.values[key]
        return 1


def _gatewayd_client(*, is_mock: bool) -> tuple[KISClient, _FakeRedis]:
    client = KISClient(is_mock=is_mock)
    manager = RedisTokenManager(
        namespace=f"gatewayd-kis-{'mock' if is_mock else 'live'}"
    )
    redis = _FakeRedis()
    manager.redis_client = redis  # type: ignore[assignment]
    client._token_manager = manager
    return client, redis


@pytest.mark.asyncio
async def test_gatewayd_ensure_posts_provider_endpoint_and_rejects_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str) -> httpx.Response:
            requests.append(url)
            return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(token_issuance.httpx, "AsyncClient", FakeClient)
    gatewayd_settings = type(
        "GatewaydSettings", (), {"gatewayd_url": "http://edge:8791/"}
    )()

    await token_issuance.ensure_gatewayd_token(
        "kis-live", settings_obj=gatewayd_settings
    )

    assert requests == ["http://edge:8791/v1/tokens/kis-live/ensure"]

    class FailingClient(FakeClient):
        async def post(self, url: str) -> httpx.Response:
            return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(token_issuance.httpx, "AsyncClient", FailingClient)
    with pytest.raises(TokenIssuanceUnavailable):
        await token_issuance.ensure_gatewayd_token(
            "kis-mock", settings_obj=gatewayd_settings
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_mock", "provider"),
    [(False, "kis-live"), (True, "kis-mock")],
)
async def test_gatewayd_miss_ensures_once_reloads_redis_and_never_calls_kis_oauth(
    monkeypatch: pytest.MonkeyPatch,
    is_mock: bool,
    provider: str,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    client, redis = _gatewayd_client(is_mock=is_mock)
    oauth = AsyncMock(side_effect=AssertionError("KIS OAuth must not run"))
    monkeypatch.setattr(client, "_fetch_token", oauth)

    async def ensure_gatewayd(actual_provider: str, *, settings_obj: object) -> None:
        assert actual_provider == provider
        assert settings_obj is client._settings
        redis.values[client._token_manager._token_key] = json.dumps(
            {
                "access_token": f"{provider}-redis-token",
                "expires_at": time.time() + 3600,
            }
        )

    ensure = AsyncMock(side_effect=ensure_gatewayd)
    monkeypatch.setattr(kis_base, "ensure_gatewayd_token", ensure)

    await client._ensure_token()

    ensure.assert_awaited_once_with(provider, settings_obj=client._settings)
    oauth.assert_not_awaited()
    assert client._settings.kis_access_token == f"{provider}-redis-token"


@pytest.mark.asyncio
async def test_gatewayd_failure_propagates_without_kis_oauth_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")
    client, _redis = _gatewayd_client(is_mock=False)
    oauth = AsyncMock(side_effect=AssertionError("KIS OAuth fallback is forbidden"))
    monkeypatch.setattr(client, "_fetch_token", oauth)
    ensure = AsyncMock(side_effect=TokenIssuanceUnavailable("gatewayd unavailable"))
    monkeypatch.setattr(kis_base, "ensure_gatewayd_token", ensure)

    with pytest.raises(TokenIssuanceUnavailable, match="gatewayd unavailable"):
        await client._ensure_token()

    ensure.assert_awaited_once_with("kis-live", settings_obj=client._settings)
    oauth.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_mode_keeps_existing_kis_oauth_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "self")
    client, _redis = _gatewayd_client(is_mock=False)
    oauth = AsyncMock(return_value=("self-issued-token", 3600))
    monkeypatch.setattr(client, "_fetch_token", oauth)
    ensure = AsyncMock()
    monkeypatch.setattr(kis_base, "ensure_gatewayd_token", ensure)

    await client._ensure_token()

    oauth.assert_awaited_once()
    ensure.assert_not_awaited()
    assert client._settings.kis_access_token == "self-issued-token"
