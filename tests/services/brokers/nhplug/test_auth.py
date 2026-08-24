"""Offline tests for the physically isolated NHPLUG OAuth client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.brokers.nhplug.auth import (
    AUTH_HOST,
    AUTH_PORT,
    AUTH_REVOKE_PATH,
    AUTH_TOKEN_PATH,
    NHPlugAuthClient,
)
from app.services.brokers.nhplug.errors import NHPlugMockEndpointError

pytestmark = pytest.mark.unit


def _transport(
    payload: dict[str, Any] | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=payload or {"access_token": "test-token", "expires_in": 86_400},
        )

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_token_request_uses_only_pinned_oauth_host_port_and_path() -> None:
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    assert await client.get_access_token() == "test-token"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.host == AUTH_HOST
    assert request.url.port == AUTH_PORT
    assert request.url.path == AUTH_TOKEN_PATH
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert b"appkey=test-key" in request.content
    assert b"appsecretkey=test-secret" in request.content


@pytest.mark.asyncio
async def test_auth_token_is_reused_without_a_second_dispatch() -> None:
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    assert await client.get_access_token() == "test-token"
    assert await client.get_access_token() == "test-token"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_auth_revoke_is_the_second_and_only_other_allowed_path() -> None:
    transport, seen = _transport(payload={"rsp_cd": "00000"})
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    assert await client.revoke_access_token(access_token="test-token") == {
        "rsp_cd": "00000"
    }
    assert len(seen) == 1
    assert seen[0].url.path == AUTH_REVOKE_PATH


@pytest.mark.asyncio
async def test_non_oauth_path_is_refused_before_any_transport_or_token_work() -> None:
    transport, seen = _transport()
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    with pytest.raises(NHPlugMockEndpointError):
        await client._post_form(path="/n2/acctinfo", form={})

    assert seen == []


@pytest.mark.parametrize(
    "base_url",
    (
        "https://moapi.nhplug.com:8443",
        "https://api.nhplug.com",
        "http://api.nhplug.com:8443",
        "https://api.nhplug.com:8443/not-allowed",
    ),
)
def test_auth_constructor_rejects_every_non_exact_endpoint(base_url: str) -> None:
    with pytest.raises(NHPlugMockEndpointError):
        NHPlugAuthClient(
            app_key="test-key",
            app_secret="test-secret",
            base_url=base_url,
        )


@pytest.mark.asyncio
async def test_auth_does_not_follow_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://unexpected.example.invalid/oauth2/token"},
        )

    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_access_token()

    assert len(seen) == 1
    assert seen[0].url.host == AUTH_HOST


@pytest.mark.asyncio
async def test_auth_postbuild_http_scheme_tamper_is_rejected_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, seen = _transport()
    original_build_request = httpx.AsyncClient.build_request

    def build_http_request(
        self: httpx.AsyncClient, *args: Any, **kwargs: Any
    ) -> httpx.Request:
        request = original_build_request(self, *args, **kwargs)
        return httpx.Request(
            request.method,
            "http://api.nhplug.com:8443/oauth2/token",
            headers=request.headers,
            content=request.content,
        )

    monkeypatch.setattr(httpx.AsyncClient, "build_request", build_http_request)
    client = NHPlugAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        transport=transport,
    )

    with pytest.raises(NHPlugMockEndpointError):
        await client.get_access_token()

    assert seen == []
