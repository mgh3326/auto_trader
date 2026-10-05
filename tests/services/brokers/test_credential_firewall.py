"""#1257: the context-scoped broker credential firewall.

Inside ``broker_credentials_blocked`` a KIS, Toss or authenticated Upbit
client refuses before any token lookup, breaker lease, rate-limit wait or
send; outside it nothing changes. Fakes only: no network, no Redis.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.brokers.credential_firewall import (
    BrokerCredentialsBlocked,
    assert_broker_credentials_allowed,
    broker_credentials_blocked,
    broker_credentials_blocked_by,
)

pytestmark = pytest.mark.unit

OWNER = "h3-us-paper"


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sync(self, label: str) -> Any:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(label)
            raise AssertionError(f"reached past the firewall: {label}")

        return record

    def asynchronous(self, label: str) -> Any:
        async def record(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(label)
            raise AssertionError(f"reached past the firewall: {label}")

        return record


def test_context_sets_and_resets_the_owner() -> None:
    assert broker_credentials_blocked_by() is None
    with broker_credentials_blocked(OWNER):
        assert broker_credentials_blocked_by() == OWNER
        with pytest.raises(BrokerCredentialsBlocked, match="h3-us-paper: X"):
            assert_broker_credentials_allowed("X")
    assert broker_credentials_blocked_by() is None
    assert_broker_credentials_allowed("X")


def test_context_resets_after_an_exception() -> None:
    with pytest.raises(RuntimeError), broker_credentials_blocked(OWNER):
        raise RuntimeError("body failed")
    assert broker_credentials_blocked_by() is None


@pytest.mark.asyncio
async def test_tasks_created_inside_the_context_inherit_it() -> None:
    async def child() -> str | None:
        await asyncio.sleep(0)
        return broker_credentials_blocked_by()

    with broker_credentials_blocked(OWNER):
        inherited = await asyncio.gather(asyncio.create_task(child()), child())
    assert inherited == [OWNER, OWNER]
    assert await child() is None


# --- KIS ------------------------------------------------------------------------


def test_kis_client_construction_is_refused_inside_only() -> None:
    from app.services.brokers.kis.client import KISClient

    KISClient()  # outside: unchanged
    with (
        broker_credentials_blocked(OWNER),
        pytest.raises(BrokerCredentialsBlocked, match="KIS client"),
    ):
        KISClient()
    with (
        broker_credentials_blocked(OWNER),
        pytest.raises(BrokerCredentialsBlocked, match="KIS client"),
    ):
        KISClient(is_mock=True)


@pytest.mark.asyncio
async def test_import_time_kis_client_refuses_before_its_token_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # app.services.brokers.kis.client.kis is built at import, outside any
    # context; its token lookup and dispatch still refuse inside one.
    from app.services.brokers.kis import base as kis_base
    from app.services.brokers.kis import client as kis_client

    recorder = _Recorder()
    monkeypatch.setattr(
        kis_base.redis_token_manager, "get_token", recorder.asynchronous("token")
    )
    monkeypatch.setattr(
        kis_base, "get_kis_circuit_breaker", recorder.sync("breaker_lease")
    )
    with broker_credentials_blocked(OWNER):
        with pytest.raises(BrokerCredentialsBlocked, match="KIS token"):
            await kis_client.kis._ensure_token()
        with pytest.raises(BrokerCredentialsBlocked, match="KIS request"):
            await kis_client.kis._request_with_rate_limit(
                "GET", "https://example.invalid/uapi/x", headers={}
            )
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_kis_paths_are_unchanged_outside_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-vacuity: without the context the same calls reach the token lookup
    # and the breaker lease (trapped here).
    from app.services.brokers.kis import base as kis_base
    from app.services.brokers.kis import client as kis_client

    recorder = _Recorder()
    monkeypatch.setattr(
        kis_base.redis_token_manager, "get_token", recorder.asynchronous("token")
    )
    monkeypatch.setattr(
        kis_base, "get_kis_circuit_breaker", recorder.sync("breaker_lease")
    )
    with pytest.raises(AssertionError):
        await kis_client.kis._ensure_token()
    with pytest.raises(AssertionError):
        await kis_client.kis._request_with_rate_limit(
            "GET", "https://example.invalid/uapi/x", headers={}
        )
    assert recorder.calls == ["token", "breaker_lease"]


# --- Toss -----------------------------------------------------------------------


class _FakeTokenManager:
    def __init__(self, recorder: _Recorder) -> None:
        self.get_access_token = recorder.asynchronous("toss_token")


class _FakeLimiter:
    def __init__(self, recorder: _Recorder) -> None:
        self.acquire = recorder.asynchronous("toss_rate_limit")


def test_toss_client_construction_is_refused_inside_only() -> None:
    from app.services.brokers.toss.client import TossReadClient

    recorder = _Recorder()
    TossReadClient(
        token_manager=_FakeTokenManager(recorder),  # type: ignore[arg-type]
        rate_limiter=_FakeLimiter(recorder),  # type: ignore[arg-type]
    )
    with (
        broker_credentials_blocked(OWNER),
        pytest.raises(BrokerCredentialsBlocked, match="Toss client"),
    ):
        TossReadClient(
            token_manager=_FakeTokenManager(recorder),  # type: ignore[arg-type]
            rate_limiter=_FakeLimiter(recorder),  # type: ignore[arg-type]
        )
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_toss_request_refuses_before_rate_limit_and_token() -> None:
    from app.services.brokers.toss.client import TossReadClient
    from app.services.brokers.toss.rate_limiter import TossApiGroup

    recorder = _Recorder()
    client = TossReadClient(
        token_manager=_FakeTokenManager(recorder),  # type: ignore[arg-type]
        rate_limiter=_FakeLimiter(recorder),  # type: ignore[arg-type]
    )
    group = next(iter(TossApiGroup))
    with (
        broker_credentials_blocked(OWNER),
        pytest.raises(BrokerCredentialsBlocked, match="Toss request"),
    ):
        await client._request("GET", "/x", group=group)
    assert recorder.calls == []
    # Non-vacuity: outside the context the rate limiter is reached first.
    with pytest.raises(AssertionError):
        await client._request("GET", "/x", group=group)
    assert recorder.calls == ["toss_rate_limit"]
    await client.aclose()


# --- Upbit ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upbit_authenticated_request_refuses_before_its_limiter(
    monkeypatch: pytest.MonkeyPatch, allow_external_providers: None
) -> None:
    # allow_external_providers keeps the real _request_with_auth (the suite
    # otherwise stubs it); the limiter trap stops it before any send, and the
    # socket guard stays active.
    from app.services.brokers.upbit import client as upbit_client

    recorder = _Recorder()
    monkeypatch.setattr(upbit_client, "get_limiter", recorder.asynchronous("limiter"))
    with (
        broker_credentials_blocked(OWNER),
        pytest.raises(BrokerCredentialsBlocked, match="Upbit authenticated request"),
    ):
        await upbit_client._request_with_auth(
            "GET", "https://api.upbit.com/v1/accounts"
        )
    assert recorder.calls == []
    with pytest.raises(AssertionError):
        await upbit_client._request_with_auth(
            "GET", "https://api.upbit.com/v1/accounts"
        )
    assert recorder.calls == ["limiter"]
