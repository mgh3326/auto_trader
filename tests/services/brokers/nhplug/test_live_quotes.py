"""Offline contracts for the NHPLUG live read-only period client.

Every HTTP interaction uses ``httpx.MockTransport``.  These tests never have
credentials and never reach NHPLUG.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from app.services.brokers.nhplug.live_quotes import (
    LIVE_TOKEN_PATH,
    US_PERIOD_PATH,
    NHPlugLiveQuotesClient,
    NHPlugLiveQuotesDisabled,
    NHPlugLiveQuotesEndpointError,
)

pytestmark = pytest.mark.unit


def _write_cached_token(path: Path, *, token: str = "cached-token") -> None:
    path.write_text(
        json.dumps({"access_token": token, "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_client_exposes_no_identity_scoped_attribute_or_constructor_argument(
    tmp_path: Path,
) -> None:
    client = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=tmp_path / "token-cache.json",
    )

    assert not [name for name in vars(client) if "account" in name.casefold()]
    assert not [name for name in dir(client) if "account" in name.casefold()]
    with pytest.raises(TypeError):
        NHPlugLiveQuotesClient(  # type: ignore[call-arg]
            app_key="stub-key",
            app_secret="stub-secret",
            token_cache_path=tmp_path / "other-token-cache.json",
            account_no="fixture",
        )


@pytest.mark.asyncio
async def test_gate_refuses_before_token_cache_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NHPLUG_LIVE_QUOTES_ENABLED", raising=False)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    client = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=tmp_path / "token-cache.json",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(NHPlugLiveQuotesDisabled):
        await client.fetch_us_period(symbol="AAPL", end_date="20260831", bars=30)

    assert calls == []


@pytest.mark.asyncio
async def test_scoped_env_client_rechecks_the_gate_at_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arming while constructing a CLI client never pins a later gate change."""

    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")
    monkeypatch.setenv("NHPLUG_LIVE_APP_KEY", "stub-key")
    monkeypatch.setenv("NHPLUG_LIVE_APP_SECRET", "stub-secret")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    client = NHPlugLiveQuotesClient.from_scoped_env(
        token_cache_path=tmp_path / "token-cache.json",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "false")

    with pytest.raises(NHPlugLiveQuotesDisabled):
        await client.fetch_us_period(symbol="AAPL", end_date="20260831", bars=30)

    assert calls == []


@pytest.mark.asyncio
async def test_non_allowlisted_fixture_path_is_refused_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    client = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=tmp_path / "token-cache.json",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(NHPlugLiveQuotesEndpointError):
        await client._post_data(  # noqa: SLF001 - exercise the pre-send boundary
            path="/gbstock/order/v1/buy", input_0={}
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered_url",
    (
        "https://elsewhere.invalid:8443/gbstock/quote/v1/period",
        "https://api.nhplug.com:8443/gbstock/order/v1/buy",
    ),
)
async def test_built_request_is_revalidated_immediately_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tampered_url: str
) -> None:
    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")
    cache_path = tmp_path / "token-cache.json"
    _write_cached_token(cache_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"Output_1": []})

    original_build = httpx.AsyncClient.build_request

    def tampered_build(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: object
    ) -> httpx.Request:
        return original_build(self, method, tampered_url, **kwargs)

    client = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=cache_path,
        transport=httpx.MockTransport(handler),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.AsyncClient, "build_request", tampered_build)
        with pytest.raises(NHPlugLiveQuotesEndpointError):
            await client.fetch_us_period(symbol="AAPL", end_date="20260831", bars=30)

    assert calls == []


@pytest.mark.asyncio
async def test_redirect_is_not_followed_to_a_non_quote_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")
    cache_path = tmp_path / "token-cache.json"
    _write_cached_token(cache_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            307,
            headers={"location": "https://api.nhplug.com:8443/gbstock/order/v1/buy"},
        )

    client = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=cache_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_us_period(symbol="AAPL", end_date="20260831", bars=30)

    assert calls == [US_PERIOD_PATH]


@pytest.mark.asyncio
async def test_shared_file_cache_suppresses_second_token_issuance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")
    cache_path = tmp_path / "token-cache.json"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == LIVE_TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": "issued-token", "expires_in": 3600}
            )
        assert request.url.path == US_PERIOD_PATH
        return httpx.Response(200, json={"Output_1": []})

    transport = httpx.MockTransport(handler)
    first = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=cache_path,
        transport=transport,
    )
    second = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=cache_path,
        transport=transport,
    )

    await first.fetch_us_period(symbol="AAPL", end_date="20260831", bars=30)
    await second.fetch_us_period(symbol="MSFT", end_date="20260831", bars=30)

    assert calls.count(LIVE_TOKEN_PATH) == 1
    assert calls.count(US_PERIOD_PATH) == 2
    assert os.stat(cache_path).st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_rate_limit_response_keeps_the_cached_token_and_never_reissues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vendor allows token reissuance for 401 only, never a 429."""

    monkeypatch.setenv("NHPLUG_LIVE_QUOTES_ENABLED", "true")
    cache_path = tmp_path / "token-cache.json"
    _write_cached_token(cache_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={"message": {"usr_msg": "rate limited"}})

    client = NHPlugLiveQuotesClient(
        app_key="stub-key",
        app_secret="stub-secret",
        token_cache_path=cache_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_us_period(symbol="AAPL", end_date="20260831", bars=30)

    assert calls == [US_PERIOD_PATH]
    assert (
        json.loads(cache_path.read_text(encoding="utf-8"))["access_token"]
        == "cached-token"
    )
