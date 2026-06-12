from __future__ import annotations

import httpx
import pytest

from app.services.brokers.toss.errors import TossHostBlocked
from app.services.brokers.toss.transport import (
    DEFAULT_TOSS_BASE_URL,
    _on_request,
    _on_response,
    build_toss_client,
)


def test_build_toss_client_accepts_default_base_url() -> None:
    client = build_toss_client()
    try:
        assert str(client.base_url) == DEFAULT_TOSS_BASE_URL
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_build_toss_client_rejects_other_host() -> None:
    with pytest.raises(TossHostBlocked):
        build_toss_client(base_url="https://evil.example.com")


def test_build_toss_client_rejects_subdomain_spoof() -> None:
    with pytest.raises(TossHostBlocked):
        build_toss_client(base_url="https://openapi.tossinvest.com.evil.example")


@pytest.mark.asyncio
async def test_on_request_rejects_absolute_url_to_other_host() -> None:
    request = httpx.Request("GET", "https://evil.example.com/api/v1/accounts")

    with pytest.raises(TossHostBlocked):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_accepts_toss_host() -> None:
    request = httpx.Request("GET", "https://openapi.tossinvest.com/api/v1/accounts")

    await _on_request(request)


@pytest.mark.asyncio
async def test_on_response_rejects_redirect() -> None:
    request = httpx.Request("GET", "https://openapi.tossinvest.com/api/v1/accounts")
    response = httpx.Response(
        302,
        headers={"location": "https://evil.example.com/api/v1/accounts"},
        request=request,
    )

    with pytest.raises(TossHostBlocked):
        await _on_response(response)
