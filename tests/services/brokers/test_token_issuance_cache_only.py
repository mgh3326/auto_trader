"""cache_only broker token mode: read the shared cache, never issue (hk 4309).

Each provider path (KIS live, KIS mock, Toss OAuth, KIS WebSocket approval keys)
gets the same three checks:

* a usable cached token is returned with no issuance side effect;
* a miss, an expired value, and a forced re-issue raise
  ``TokenIssuanceCacheOnlyUnavailable`` and reach no OAuth/approval HTTP request,
  no gatewayd ensure, no issuance lock, and no Redis write or delete.

The Redis double records every mutation and raises on it, so a path that falls
through to issuance fails here instead of silently succeeding.
"""

from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, settings
from app.services.brokers.kis import base as kis_base
from app.services.brokers.kis.client import KISClient
from app.services.brokers.token_issuance import (
    TokenIssuanceCacheOnlyUnavailable,
    TokenIssuanceUnavailable,
    is_cache_only_token_issuance,
)
from app.services.brokers.toss import auth as toss_auth
from app.services.invest_home_readers import SafeKISMockClient
from app.services.kis_websocket_internal import approval_keys
from app.services.kis_websocket_internal.client import KISExecutionWebSocket
from app.services.kis_websocket_internal.protocol import (
    DOMESTIC_EXECUTION_TR_REAL,
    KISSubscriptionAckError,
)
from app.services.redis_token_manager import RedisTokenManager


class _TrapRedis:
    """Reads are allowed. Every write, delete, eval, or lock attempt is recorded and raises."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})
        self.mutations: list[str] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *args: Any, **kwargs: Any) -> bool:
        self.mutations.append(f"set {key}")
        raise AssertionError("cache_only must not write Redis (set)")

    async def delete(self, *keys: str) -> int:
        self.mutations.append(f"delete {keys}")
        raise AssertionError("cache_only must not delete Redis keys")

    async def eval(self, *args: Any) -> int:
        self.mutations.append("eval")
        raise AssertionError("cache_only must not run Redis scripts")

    async def execute_command(self, *args: Any) -> int:
        self.mutations.append("execute_command")
        raise AssertionError("cache_only must not run Redis commands")


@pytest.fixture
def cache_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "cache_only")
    # Restore the in-process KIS token copy that _ensure_token writes on a hit.
    monkeypatch.setattr(settings, "kis_access_token", None)


@pytest.fixture
def no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Forbidden:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("cache_only must not open an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _Forbidden)


@pytest.fixture
def gatewayd_ensure(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    ensure = AsyncMock(side_effect=AssertionError("cache_only must not call gatewayd"))
    monkeypatch.setattr(kis_base, "ensure_gatewayd_token", ensure)
    monkeypatch.setattr(toss_auth, "ensure_gatewayd_token", ensure)
    return ensure


# --- settings and predicate -------------------------------------------------


def test_default_mode_stays_self_and_cache_only_is_accepted() -> None:
    common = {
        "kis_app_key": "kis-key",
        "kis_app_secret": "kis-secret",
        "opendart_api_key": "dart-key",
        "upbit_access_key": "upbit-key",
        "upbit_secret_key": "upbit-secret",
        "SECRET_KEY": "TestSecret123-" + "x" * 32,
    }

    assert Settings(**common).broker_token_issuance_mode == "self"
    assert (
        Settings(
            **common, broker_token_issuance_mode="cache_only"
        ).broker_token_issuance_mode
        == "cache_only"
    )


def test_unknown_mode_is_still_rejected() -> None:
    common = {
        "kis_app_key": "kis-key",
        "kis_app_secret": "kis-secret",
        "opendart_api_key": "dart-key",
        "upbit_access_key": "upbit-key",
        "upbit_secret_key": "upbit-secret",
        "SECRET_KEY": "TestSecret123-" + "x" * 32,
    }

    with pytest.raises(ValidationError):
        Settings(**common, broker_token_issuance_mode="issue")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("self", False), ("gatewayd", False), ("cache_only", True)],
)
def test_predicate_is_true_only_for_cache_only(mode: str, expected: bool) -> None:
    assert (
        is_cache_only_token_issuance(SimpleNamespace(broker_token_issuance_mode=mode))
        is expected
    )


def test_refusal_is_a_token_issuance_unavailable() -> None:
    assert issubclass(TokenIssuanceCacheOnlyUnavailable, TokenIssuanceUnavailable)


# --- KIS live and mock access tokens ----------------------------------------


def _kis_client(*, is_mock: bool, redis: _TrapRedis) -> KISClient:
    client = KISClient(is_mock=is_mock)
    manager = RedisTokenManager(
        namespace=f"cache-only-kis-{'mock' if is_mock else 'live'}"
    )
    manager.redis_client = redis  # type: ignore[assignment]
    client._token_manager = manager
    return client


def _kis_token_json(access_token: str, *, expires_at: float) -> str:
    return json.dumps({"access_token": access_token, "expires_at": expires_at})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_mock", "provider"), [(False, "kis-live"), (True, "kis-mock")]
)
async def test_kis_cache_only_returns_cached_token_without_issuance(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    is_mock: bool,
    provider: str,
) -> None:
    redis = _TrapRedis()
    client = _kis_client(is_mock=is_mock, redis=redis)
    redis.values[client._token_manager._token_key] = _kis_token_json(
        f"{provider}-cached", expires_at=time.time() + 3600
    )
    oauth = AsyncMock(side_effect=AssertionError("KIS OAuth must not run"))
    monkeypatch.setattr(client, "_fetch_token", oauth)

    await client._ensure_token()

    assert client._settings.kis_access_token == f"{provider}-cached"
    oauth.assert_not_awaited()
    gatewayd_ensure.assert_not_awaited()
    assert redis.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_mock", "provider"), [(False, "kis-live"), (True, "kis-mock")]
)
async def test_kis_cache_only_miss_refuses_without_issuance(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    is_mock: bool,
    provider: str,
) -> None:
    redis = _TrapRedis()
    client = _kis_client(is_mock=is_mock, redis=redis)
    oauth = AsyncMock(side_effect=AssertionError("KIS OAuth must not run"))
    monkeypatch.setattr(client, "_fetch_token", oauth)

    with pytest.raises(
        TokenIssuanceCacheOnlyUnavailable, match=re.escape(f"provider={provider}:")
    ):
        await client._ensure_token()

    oauth.assert_not_awaited()
    gatewayd_ensure.assert_not_awaited()
    assert redis.mutations == []
    assert redis.values == {}


@pytest.mark.asyncio
async def test_kis_cache_only_expired_token_refuses_and_leaves_value_untouched(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _TrapRedis()
    client = _kis_client(is_mock=False, redis=redis)
    key = client._token_manager._token_key
    expired = _kis_token_json("SECRET-EXPIRED-KIS", expires_at=time.time() - 60)
    redis.values[key] = expired
    oauth = AsyncMock(side_effect=AssertionError("KIS OAuth must not run"))
    monkeypatch.setattr(client, "_fetch_token", oauth)

    with pytest.raises(TokenIssuanceCacheOnlyUnavailable) as exc:
        await client._ensure_token()

    oauth.assert_not_awaited()
    gatewayd_ensure.assert_not_awaited()
    assert redis.mutations == []
    assert redis.values[key] == expired
    # The message names provider and mode only: no token value, no URL.
    message = str(exc.value)
    assert "cache_only" in message
    assert "provider=kis-live" in message
    assert "SECRET-EXPIRED-KIS" not in message
    assert "://" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_token", ["SECRET-VALID-KIS", None])
async def test_kis_forced_invalidation_refuses_without_redis_mutation(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    failed_token: str | None,
) -> None:
    """KIS call sites run clear_token(failed) before re-ensuring; that is the forced path."""
    redis = _TrapRedis()
    client = _kis_client(is_mock=False, redis=redis)
    key = client._token_manager._token_key
    redis.values[key] = _kis_token_json(
        "SECRET-VALID-KIS", expires_at=time.time() + 3600
    )
    before = dict(redis.values)

    with pytest.raises(
        TokenIssuanceCacheOnlyUnavailable, match=re.escape("provider=kis:")
    ):
        await client._token_manager.clear_token(failed_token)

    assert redis.mutations == []
    assert redis.values == before
    gatewayd_ensure.assert_not_awaited()


# --- Toss OAuth token -------------------------------------------------------


@pytest.fixture
def toss_redis(monkeypatch: pytest.MonkeyPatch) -> _TrapRedis:
    redis = _TrapRedis()

    async def get_client() -> _TrapRedis:
        return redis

    monkeypatch.setattr(toss_auth, "_get_redis_client", get_client)
    return redis


def _toss_manager() -> toss_auth.TossOAuthTokenManager:
    return toss_auth.TossOAuthTokenManager(
        client_id="cache-only-client",
        client_secret=SecretStr("cache-only-secret"),
    )


def _toss_token_json(access_token: str, *, expires_at: float) -> str:
    return json.dumps({"access_token": access_token, "expires_at": expires_at})


@pytest.mark.asyncio
async def test_toss_cache_only_returns_cached_token_without_issuance(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    toss_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _toss_manager()
    toss_redis.values[manager.token_key] = _toss_token_json(
        "SECRET-TOSS-CACHED", expires_at=time.time() + 3600
    )
    issue = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", issue)

    assert await manager.get_access_token() == "SECRET-TOSS-CACHED"

    issue.assert_not_awaited()
    gatewayd_ensure.assert_not_awaited()
    assert toss_redis.mutations == []


@pytest.mark.asyncio
async def test_toss_cache_only_miss_refuses_without_lock_or_oauth(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    toss_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _toss_manager()
    issue = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", issue)

    with pytest.raises(
        TokenIssuanceCacheOnlyUnavailable, match=re.escape("provider=toss:")
    ):
        await manager.get_access_token()

    issue.assert_not_awaited()
    gatewayd_ensure.assert_not_awaited()
    assert manager.lock_key not in toss_redis.values
    assert toss_redis.mutations == []


@pytest.mark.asyncio
async def test_toss_cache_only_expired_token_refuses_and_leaves_value_untouched(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    toss_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _toss_manager()
    expired = _toss_token_json("SECRET-TOSS-EXPIRED", expires_at=time.time() - 60)
    toss_redis.values[manager.token_key] = expired
    issue = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", issue)

    with pytest.raises(TokenIssuanceCacheOnlyUnavailable) as exc:
        await manager.get_access_token()

    issue.assert_not_awaited()
    assert toss_redis.mutations == []
    assert toss_redis.values[manager.token_key] == expired
    assert "SECRET-TOSS-EXPIRED" not in str(exc.value)


@pytest.mark.asyncio
async def test_toss_forced_reissue_with_valid_cache_refuses_without_mutation(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    toss_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _toss_manager()
    cached = _toss_token_json("SECRET-TOSS-VALID", expires_at=time.time() + 3600)
    toss_redis.values[manager.token_key] = cached
    issue = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", issue)

    # Forced with no failed token: must not delete or reissue the valid cache.
    with pytest.raises(TokenIssuanceCacheOnlyUnavailable):
        await manager.get_access_token(force_reissue=True)
    # Forced with the cached token as the failed one: refused as well.
    with pytest.raises(TokenIssuanceCacheOnlyUnavailable):
        await manager.get_access_token(
            force_reissue=True, failed_token="SECRET-TOSS-VALID"
        )

    issue.assert_not_awaited()
    gatewayd_ensure.assert_not_awaited()
    assert toss_redis.mutations == []
    assert toss_redis.values[manager.token_key] == cached


@pytest.mark.asyncio
async def test_toss_forced_reissue_reuses_a_peer_token_as_a_cache_hit(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    toss_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer already replaced the failed token: a usable cache hit, as in self mode."""
    manager = _toss_manager()
    toss_redis.values[manager.token_key] = _toss_token_json(
        "SECRET-TOSS-PEER", expires_at=time.time() + 3600
    )
    issue = AsyncMock(side_effect=AssertionError("Toss OAuth must not run"))
    monkeypatch.setattr(manager, "_issue_token", issue)

    token = await manager.get_access_token(
        force_reissue=True, failed_token="SECRET-TOSS-OLD"
    )

    assert token == "SECRET-TOSS-PEER"
    issue.assert_not_awaited()
    assert toss_redis.mutations == []


# --- KIS WebSocket approval keys --------------------------------------------


@pytest.fixture
def approval_redis(monkeypatch: pytest.MonkeyPatch) -> _TrapRedis:
    redis = _TrapRedis()

    async def get_client() -> _TrapRedis:
        return redis

    monkeypatch.setattr(approval_keys, "_get_redis_client", get_client)
    return redis


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
async def test_approval_key_cache_only_returns_cached_key_without_issuance(
    cache_only_mode: None,
    no_http: None,
    approval_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
) -> None:
    approval_redis.values[approval_keys.APPROVAL_KEY_CACHE_KEYS[account_mode]] = (
        "SECRET-AK-CACHED"
    )
    issue = AsyncMock(side_effect=AssertionError("approval endpoint must not run"))
    monkeypatch.setattr(approval_keys, "_issue_approval_key", issue)

    assert await approval_keys.get_approval_key(account_mode) == "SECRET-AK-CACHED"

    issue.assert_not_awaited()
    assert approval_redis.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
@pytest.mark.parametrize("cached", [None, "   "], ids=["missing-or-expired", "blank"])
async def test_approval_key_cache_only_miss_refuses_without_lock_or_http(
    cache_only_mode: None,
    no_http: None,
    approval_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
    cached: str | None,
) -> None:
    # An approval key carries no expiry in its value; Redis TTL removes it, so
    # expiry is observed as a missing (or blank) key.
    cache_key = approval_keys.APPROVAL_KEY_CACHE_KEYS[account_mode]
    if cached is not None:
        approval_redis.values[cache_key] = cached
    issue = AsyncMock(side_effect=AssertionError("approval endpoint must not run"))
    monkeypatch.setattr(approval_keys, "_issue_approval_key", issue)

    with pytest.raises(
        TokenIssuanceCacheOnlyUnavailable,
        match=re.escape(f"provider=kis-websocket-approval-{account_mode}:"),
    ):
        await approval_keys.get_approval_key(account_mode)

    issue.assert_not_awaited()
    assert approval_keys.APPROVAL_KEY_LOCK_CACHE_KEYS[account_mode] not in (
        approval_redis.values
    )
    assert approval_redis.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
async def test_approval_key_forced_reissue_refuses_without_cache_delete(
    cache_only_mode: None,
    no_http: None,
    approval_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
) -> None:
    cache_key = approval_keys.APPROVAL_KEY_CACHE_KEYS[account_mode]
    approval_redis.values[cache_key] = "SECRET-AK-VALID"
    issue = AsyncMock(side_effect=AssertionError("approval endpoint must not run"))
    monkeypatch.setattr(approval_keys, "_issue_approval_key", issue)

    with pytest.raises(TokenIssuanceCacheOnlyUnavailable):
        await approval_keys.invalidate_and_reissue_approval_key(account_mode)

    issue.assert_not_awaited()
    assert approval_redis.mutations == []
    assert approval_redis.values[cache_key] == "SECRET-AK-VALID"


@pytest.mark.asyncio
async def test_approval_endpoint_call_is_refused_even_when_called_directly(
    cache_only_mode: None,
    no_http: None,
) -> None:
    with pytest.raises(TokenIssuanceCacheOnlyUnavailable):
        await approval_keys._issue_approval_key("kis_live")


# --- PR 2389 review regressions ---------------------------------------------


@pytest.mark.asyncio
async def test_kis_rejected_token_is_not_recached_and_producer_replacement_is_observed(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
) -> None:
    """KIS rejects cached token A. The producer later publishes B; this process must return B."""
    redis = _TrapRedis()
    manager = RedisTokenManager(namespace="cache-only-kis-rejected")
    manager.redis_client = redis  # type: ignore[assignment]
    key = manager._token_key
    redis.values[key] = _kis_token_json("SECRET-TOKEN-A", expires_at=time.time() + 3600)

    assert await manager.get_token() == "SECRET-TOKEN-A"
    with pytest.raises(TokenIssuanceCacheOnlyUnavailable):
        await manager.clear_token("SECRET-TOKEN-A")
    # Redis still holds A until the producer rotates. It must not come back locally.
    assert await manager.get_token() is None
    assert await manager.get_token() is None

    redis.values[key] = _kis_token_json("SECRET-TOKEN-B", expires_at=time.time() + 3600)
    assert await manager.get_token() == "SECRET-TOKEN-B"
    assert redis.mutations == []
    gatewayd_ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_kis_mock_client_refusal_names_kis_mock(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SafeKISMockClient never sets _is_mock_client, but its token namespace is kis_mock."""
    monkeypatch.setattr(
        settings, "kis_mock_base_url", "https://openapivts.koreainvestment.com:29443"
    )
    monkeypatch.setattr(settings, "kis_mock_app_key", "mock-app-key-for-test")
    client = SafeKISMockClient()
    redis = _TrapRedis()
    manager = RedisTokenManager(namespace="cache-only-kis-safe-mock")
    manager.redis_client = redis  # type: ignore[assignment]
    client._token_manager = manager

    with pytest.raises(
        TokenIssuanceCacheOnlyUnavailable, match=re.escape("provider=kis-mock:")
    ):
        await client._ensure_token()

    assert redis.mutations == []
    gatewayd_ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_reconnect_backs_off_when_cache_only_refuses_reissue(
    cache_only_mode: None,
    no_http: None,
    approval_redis: _TrapRedis,
) -> None:
    """The refusal must follow the back-off path and keep the reconnect loop alive."""
    client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=False)
    client.is_running = True
    client.reconnect_delay = 0
    client.max_reconnect_attempts = 3
    client.approval_key = "cached-key"
    attempts = 0

    async def connect_fail_then_success() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KISSubscriptionAckError(
                tr_id=DOMESTIC_EXECUTION_TR_REAL,
                rt_cd="1",
                msg_cd="OPSP0011",
                msg1="invalid approval : NOT FOUND",
            )
        client.is_connected = True

    with (
        patch.object(
            client,
            "_issue_approval_key_if_needed",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client,
            "_build_websocket_url",
            new=AsyncMock(return_value="ws://ops.koreainvestment.com:21000/tryitout"),
        ),
        patch.object(
            client,
            "_connect_and_subscribe_internal",
            new=AsyncMock(side_effect=connect_fail_then_success),
        ),
        patch.object(client, "_close_websocket_best_effort", new=AsyncMock()),
    ):
        await client.connect_and_subscribe()

    assert attempts == 2
    assert approval_redis.mutations == []


@pytest.mark.asyncio
async def test_kis_forced_invalidation_returns_when_producer_already_replaced_token(
    cache_only_mode: None,
    no_http: None,
    gatewayd_ensure: AsyncMock,
) -> None:
    """The producer rotated between KIS's rejection and clear_token: a cache hit, no raise."""
    redis = _TrapRedis()
    manager = RedisTokenManager(namespace="cache-only-kis-peer-replaced")
    manager.redis_client = redis  # type: ignore[assignment]
    key = manager._token_key
    redis.values[key] = _kis_token_json("SECRET-TOKEN-A", expires_at=time.time() + 3600)
    assert await manager.get_token() == "SECRET-TOKEN-A"

    redis.values[key] = _kis_token_json("SECRET-TOKEN-B", expires_at=time.time() + 3600)
    await manager.clear_token("SECRET-TOKEN-A")  # must not raise

    assert await manager.get_token() == "SECRET-TOKEN-B"
    assert redis.mutations == []
    gatewayd_ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_reconnect_adopts_producer_key_published_after_cache_only_refusal(
    cache_only_mode: None,
    no_http: None,
    approval_redis: _TrapRedis,
) -> None:
    """Refused reissue keeps the loop alive; a key published later must be adopted."""
    cache_key = approval_keys.APPROVAL_KEY_CACHE_KEYS["kis_live"]
    client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=False)
    client.is_running = True
    client.reconnect_delay = 0
    client.max_reconnect_attempts = 4
    client.approval_key = "KEY-REJECTED"
    keys_used: list[str] = []

    async def connect() -> None:
        keys_used.append(client.approval_key)
        if len(keys_used) < 3:
            if len(keys_used) == 2:
                # The producer rotates the key between the two rejections.
                approval_redis.values[cache_key] = "KEY-ROTATED"
            raise KISSubscriptionAckError(
                tr_id=DOMESTIC_EXECUTION_TR_REAL,
                rt_cd="1",
                msg_cd="OPSP0011",
                msg1="invalid approval : NOT FOUND",
            )
        client.is_connected = True

    with (
        patch.object(
            client,
            "_issue_approval_key_if_needed",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client,
            "_build_websocket_url",
            new=AsyncMock(return_value="ws://ops.koreainvestment.com:21000/tryitout"),
        ),
        patch.object(
            client,
            "_connect_and_subscribe_internal",
            new=AsyncMock(side_effect=connect),
        ),
        patch.object(client, "_close_websocket_best_effort", new=AsyncMock()),
    ):
        await client.connect_and_subscribe()

    assert keys_used == ["KEY-REJECTED", "KEY-REJECTED", "KEY-ROTATED"]
    assert client.approval_key == "KEY-ROTATED"
    assert approval_redis.mutations == []


@pytest.mark.asyncio
async def test_websocket_reconnect_survives_redis_error_during_key_refresh(
    cache_only_mode: None,
    no_http: None,
    approval_redis: _TrapRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis read error during the cache_only key refresh keeps the loop alive."""
    client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=False)
    client.is_running = True
    client.reconnect_delay = 0
    client.max_reconnect_attempts = 4
    client.approval_key = "KEY-REJECTED"
    keys_used: list[str] = []

    async def broken_get(key: str) -> str | None:
        raise ConnectionError("redis unavailable")

    async def connect() -> None:
        keys_used.append(client.approval_key)
        if len(keys_used) < 3:
            if len(keys_used) == 2:
                # Redis fails on the repeated-ACK refresh path.
                monkeypatch.setattr(approval_redis, "get", broken_get)
            raise KISSubscriptionAckError(
                tr_id=DOMESTIC_EXECUTION_TR_REAL,
                rt_cd="1",
                msg_cd="OPSP0011",
                msg1="invalid approval : NOT FOUND",
            )
        client.is_connected = True

    with (
        patch.object(
            client,
            "_issue_approval_key_if_needed",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client,
            "_build_websocket_url",
            new=AsyncMock(return_value="ws://ops.koreainvestment.com:21000/tryitout"),
        ),
        patch.object(
            client,
            "_connect_and_subscribe_internal",
            new=AsyncMock(side_effect=connect),
        ),
        patch.object(client, "_close_websocket_best_effort", new=AsyncMock()),
    ):
        await client.connect_and_subscribe()

    assert keys_used == ["KEY-REJECTED", "KEY-REJECTED", "KEY-REJECTED"]
    assert client.approval_key == "KEY-REJECTED"
    assert approval_redis.mutations == []
