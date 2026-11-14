"""gatewayd-mode KIS WebSocket approval keys (hk 4311 / TOK-T5).

In ``broker_token_issuance_mode=gatewayd`` the approval-key path must never
self-issue and never take the Python issuance lock: it reads the shared Redis
cache, POSTs the matching ``*-approval`` ensure endpoint, and re-reads Redis
with the same bounded poll the access-token path uses. The controlled reissue
path sends ``force=True`` (``?force=true``). Any transport failure, non-2xx,
or acknowledgement without a usable cached key raises
``TokenIssuanceUnavailable`` — there is no self-issuance fallback.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.brokers.token_issuance import TokenIssuanceUnavailable
from app.services.kis_websocket_internal import approval_keys

pytestmark = pytest.mark.asyncio

_ACCOUNT_MODE_TO_PROVIDER = {
    "kis_live": "kis-live-approval",
    "kis_mock": "kis-mock-approval",
}


class _GatewaydRedis:
    """Reads allowed; every Python-side mutation is recorded and raises.

    The fake ensure "publishes" by writing ``values`` directly — the same
    observation gatewayd's own Redis write produces — so it never goes
    through ``set``. Any ``set``/``delete``/``eval``/``execute_command`` call
    is therefore proof that Python tried to mutate Redis itself.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.mutations: list[str] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *args: Any, **kwargs: Any) -> bool:
        self.mutations.append(f"set {key}")
        raise AssertionError("gatewayd mode must not write Redis (set)")

    async def delete(self, *keys: str) -> int:
        self.mutations.append(f"delete {keys}")
        raise AssertionError("gatewayd mode must not delete Redis keys")

    async def eval(self, *args: Any) -> int:
        self.mutations.append("eval")
        raise AssertionError("gatewayd mode must not run Redis scripts")

    async def execute_command(self, *args: Any) -> int:
        self.mutations.append("execute_command")
        raise AssertionError("gatewayd mode must not run Redis commands")


@pytest.fixture
def gatewayd_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "broker_token_issuance_mode", "gatewayd")


@pytest.fixture
def approval_redis(monkeypatch: pytest.MonkeyPatch) -> _GatewaydRedis:
    redis = _GatewaydRedis()

    async def get_client() -> _GatewaydRedis:
        return redis

    monkeypatch.setattr(approval_keys, "_get_redis_client", get_client)
    # Keep the bounded re-read poll fast and deterministic.
    monkeypatch.setattr(approval_keys, "GATEWAYD_TOKEN_POLL_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(approval_keys, "GATEWAYD_TOKEN_POLL_INTERVAL_SECONDS", 0.01)
    return redis


@pytest.fixture
def no_self_issuance(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    issue = AsyncMock(side_effect=AssertionError("approval endpoint must not run"))
    monkeypatch.setattr(approval_keys, "_issue_approval_key", issue)
    return issue


def _cache_key(account_mode: str) -> str:
    return approval_keys.APPROVAL_KEY_CACHE_KEYS[account_mode]


def _lock_key(account_mode: str) -> str:
    return approval_keys.APPROVAL_KEY_LOCK_CACHE_KEYS[account_mode]


def _assert_no_python_side_effects(redis: _GatewaydRedis, account_mode: str) -> None:
    assert redis.mutations == []
    assert _lock_key(account_mode) not in redis.values


@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
@pytest.mark.parametrize("cached", [None, "   "], ids=["missing", "blank"])
async def test_gatewayd_miss_ensures_once_and_re_reads_published_key(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
    cached: str | None,
) -> None:
    if cached is not None:
        approval_redis.values[_cache_key(account_mode)] = cached
    provider = _ACCOUNT_MODE_TO_PROVIDER[account_mode]
    published = f"{provider}-published-key"
    calls: list[tuple[str, bool]] = []

    async def ensure(
        actual_provider: str, *, settings_obj: object, force: bool = False
    ) -> None:
        calls.append((actual_provider, force))
        assert settings_obj is settings
        # Simulate gatewayd's own Redis publish of the approval key.
        approval_redis.values[_cache_key(account_mode)] = published

    ensure_mock = AsyncMock(side_effect=ensure)
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure_mock)

    result = await approval_keys.get_approval_key(account_mode)

    assert result == published
    ensure_mock.assert_awaited_once_with(provider, settings_obj=settings, force=False)
    assert calls == [(provider, False)]
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, account_mode)


async def test_gatewayd_cache_hit_returns_without_ensure(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_redis.values[_cache_key("kis_live")] = "cached-key"
    ensure = AsyncMock(side_effect=AssertionError("ensure must not run on a hit"))
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure)

    assert await approval_keys.get_approval_key("kis_live") == "cached-key"

    ensure.assert_not_awaited()
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, "kis_live")


@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
async def test_gatewayd_reissue_ensures_with_force_and_re_reads(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
) -> None:
    # A stale key may still be cached; the forced ensure must not read-first.
    approval_redis.values[_cache_key(account_mode)] = "stale-key"
    provider = _ACCOUNT_MODE_TO_PROVIDER[account_mode]
    fresh = f"{provider}-fresh-key"

    async def ensure(
        actual_provider: str, *, settings_obj: object, force: bool = False
    ) -> None:
        assert actual_provider == provider
        assert settings_obj is settings
        assert force is True
        approval_redis.values[_cache_key(account_mode)] = fresh

    ensure_mock = AsyncMock(side_effect=ensure)
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure_mock)

    result = await approval_keys.invalidate_and_reissue_approval_key(account_mode)

    assert result == fresh
    ensure_mock.assert_awaited_once_with(provider, settings_obj=settings, force=True)
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, account_mode)


async def test_gatewayd_reissue_waits_past_stale_key_for_lagging_publish(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ensure ack can land before gatewayd's Redis publish: the poll must
    keep waiting past the pre-ensure (rejected) value instead of returning it."""
    account_mode = "kis_live"
    cache_key = _cache_key(account_mode)
    approval_redis.values[cache_key] = "rejected-key"
    fresh = "kis-live-approval-fresh-key"

    async def ensure(
        actual_provider: str, *, settings_obj: object, force: bool = False
    ) -> None:
        assert actual_provider == "kis-live-approval"
        assert force is True

        async def publish_later() -> None:
            await asyncio.sleep(0.05)
            approval_redis.values[cache_key] = fresh

        asyncio.create_task(publish_later())

    ensure_mock = AsyncMock(side_effect=ensure)
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure_mock)

    result = await approval_keys.invalidate_and_reissue_approval_key(account_mode)

    assert result == fresh
    ensure_mock.assert_awaited_once_with(
        "kis-live-approval", settings_obj=settings, force=True
    )
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, account_mode)


async def test_gatewayd_ensure_failure_raises_without_self_issuance(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure = AsyncMock(side_effect=TokenIssuanceUnavailable("gatewayd down"))
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure)

    with pytest.raises(TokenIssuanceUnavailable, match="gatewayd down"):
        await approval_keys.get_approval_key("kis_live")

    ensure.assert_awaited_once_with(
        "kis-live-approval", settings_obj=settings, force=False
    )
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, "kis_live")


async def test_gatewayd_ack_without_published_key_raises(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gatewayd acknowledged the ensure but never published a usable key.
    ensure = AsyncMock(return_value=None)
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure)

    with pytest.raises(TokenIssuanceUnavailable, match="without publishing"):
        await approval_keys.get_approval_key("kis_live")

    ensure.assert_awaited_once()
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, "kis_live")


async def test_gatewayd_force_ack_without_published_key_raises(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
    no_self_issuance: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cached value is the key KIS just rejected. gatewayd acknowledged the
    # forced ensure but never published a replacement, so the stale key must
    # NOT satisfy the poll — this is the OPSP0011 regression the differ-guard
    # exists for.
    approval_redis.values[_cache_key("kis_mock")] = "rejected-key"
    ensure = AsyncMock(return_value=None)
    monkeypatch.setattr(approval_keys, "ensure_gatewayd_token", ensure)

    with pytest.raises(TokenIssuanceUnavailable, match="without publishing"):
        await approval_keys.invalidate_and_reissue_approval_key("kis_mock")

    ensure.assert_awaited_once_with(
        "kis-mock-approval", settings_obj=settings, force=True
    )
    no_self_issuance.assert_not_awaited()
    _assert_no_python_side_effects(approval_redis, "kis_mock")


async def test_self_issuance_entry_points_refuse_in_gatewayd_mode(
    gatewayd_mode: None,
    approval_redis: _GatewaydRedis,
) -> None:
    """Defense in depth: even a direct private call cannot issue or lock."""
    with pytest.raises(TokenIssuanceUnavailable, match="mode=gatewayd"):
        await approval_keys._issue_approval_key_single_flight("kis_live")
    with pytest.raises(TokenIssuanceUnavailable, match="mode=gatewayd"):
        await approval_keys._issue_approval_key("kis_live")

    _assert_no_python_side_effects(approval_redis, "kis_live")
