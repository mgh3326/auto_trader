"""ROB-296 — Spot Demo signed-transport factory + event-hook tests.

Covers factory-time and per-request enforcement of the Spot Demo
allowlist, the 3-way cross-allowlist guard (TESTNET + PUBLIC rejected),
and the 3xx redirect refusal.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoCrossAllowlistViolation,
)
from app.services.brokers.binance.spot_demo.transport import (
    _on_request,
    _on_response,
    build_spot_demo_client,
)


def _close_client_safely(client: httpx.AsyncClient) -> None:
    """Close a client that was constructed for a fail-closed test."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(client.aclose())


def test_build_spot_demo_client_accepts_default_base_url() -> None:
    """Factory accepts the documented demo base URL."""
    client = build_spot_demo_client(
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://demo-api.binance.com",
    )
    try:
        assert str(client.base_url) == "https://demo-api.binance.com"
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_build_spot_demo_client_rejects_testnet_host() -> None:
    """Factory rejects a Spot Testnet host with the cross-allowlist exception."""
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        build_spot_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://testnet.binance.vision",
        )


def test_build_spot_demo_client_rejects_live_api_host() -> None:
    """Factory rejects live/mainnet host with cross-allowlist exception."""
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        build_spot_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://api.binance.com",
        )


def test_build_spot_demo_client_rejects_futures_demo_host() -> None:
    """Factory rejects Futures Demo host (ROB-291 scope, not here)."""
    with pytest.raises(BinanceLiveHostBlocked):
        build_spot_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://demo-fapi.binance.com",
        )


def test_build_spot_demo_client_rejects_arbitrary_host() -> None:
    """Factory rejects any host outside SPOT_DEMO_HOSTS."""
    with pytest.raises(BinanceLiveHostBlocked):
        build_spot_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://evil.example.com",
        )


def test_build_spot_demo_client_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        build_spot_demo_client(
            api_key="", api_secret="x", base_url="https://demo-api.binance.com"
        )


def test_build_spot_demo_client_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        build_spot_demo_client(
            api_key="x", api_secret="", base_url="https://demo-api.binance.com"
        )


@pytest.mark.asyncio
async def test_on_request_rejects_testnet_host() -> None:
    """Per-request hook rejects a Spot Testnet host with the cross-allowlist exception."""
    request = httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_public_host() -> None:
    """Per-request hook rejects a live/public host with the cross-allowlist exception."""
    request = httpx.Request("GET", "https://api.binance.com/api/v3/account")
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_unknown_host() -> None:
    """Per-request hook rejects an arbitrary host with BinanceLiveHostBlocked."""
    request = httpx.Request("GET", "https://evil.example.com/api/v3/account")
    with pytest.raises(BinanceLiveHostBlocked):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_accepts_spot_demo_host() -> None:
    """Per-request hook accepts the documented Spot Demo host."""
    request = httpx.Request("GET", "https://demo-api.binance.com/api/v3/account")
    await _on_request(request)  # Should not raise.


@pytest.mark.asyncio
async def test_on_response_rejects_3xx_redirect() -> None:
    """3xx redirects are refused — Spot Demo endpoints do not legitimately redirect."""
    request = httpx.Request("GET", "https://demo-api.binance.com/api/v3/account")
    response = httpx.Response(
        302,
        headers={"location": "https://api.binance.com/api/v3/account"},
        request=request,
    )
    with pytest.raises(BinanceLiveHostBlocked):
        await _on_response(response)


@pytest.mark.asyncio
async def test_on_response_accepts_2xx() -> None:
    """2xx responses pass through the post-response hook untouched."""
    request = httpx.Request("GET", "https://demo-api.binance.com/api/v3/account")
    response = httpx.Response(200, request=request)
    await _on_response(response)  # Should not raise.
