"""ROB-298 PR 2 — Futures Demo signed-transport factory + event-hook tests.

Covers factory-time and per-request enforcement of the Futures Demo
allowlist plus the cross-allowlist guard: requests must not route to
Spot Demo, Spot Testnet (deprecated), Futures Testnet (deprecated),
or any live host.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoCrossAllowlistViolation,
)
from app.services.brokers.binance.futures_demo.transport import (
    _on_request,
    _on_response,
    build_futures_demo_client,
)


def test_build_futures_demo_client_accepts_default_base_url() -> None:
    """Factory accepts the documented Futures Demo base URL."""
    client = build_futures_demo_client(
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://demo-fapi.binance.com",
    )
    try:
        assert str(client.base_url) == "https://demo-fapi.binance.com"
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_build_futures_demo_client_rejects_live_futures_host() -> None:
    """Factory rejects live Futures host with cross-allowlist exception."""
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        build_futures_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://fapi.binance.com",
        )


def test_build_futures_demo_client_rejects_live_spot_host() -> None:
    """Factory rejects live Spot host with cross-allowlist exception."""
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        build_futures_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://api.binance.com",
        )


def test_build_futures_demo_client_rejects_spot_demo_host() -> None:
    """Cross-allowlist guard: futures transport must reject Spot Demo host."""
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        build_futures_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://demo-api.binance.com",
        )


def test_build_futures_demo_client_rejects_deprecated_futures_testnet() -> None:
    """Factory rejects deprecated Futures Testnet host."""
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        build_futures_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://testnet.binancefuture.com",
        )


def test_build_futures_demo_client_rejects_deprecated_spot_testnet() -> None:
    """Factory rejects deprecated Spot Testnet host."""
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        build_futures_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://testnet.binance.vision",
        )


def test_build_futures_demo_client_rejects_arbitrary_host() -> None:
    """Factory rejects any host outside FUTURES_DEMO_HOSTS."""
    with pytest.raises(BinanceLiveHostBlocked):
        build_futures_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://evil.example.com",
        )


def test_build_futures_demo_client_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        build_futures_demo_client(
            api_key="", api_secret="x", base_url="https://demo-fapi.binance.com"
        )


def test_build_futures_demo_client_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        build_futures_demo_client(
            api_key="x", api_secret="", base_url="https://demo-fapi.binance.com"
        )


@pytest.mark.asyncio
async def test_on_request_rejects_live_futures_host() -> None:
    """Per-request hook rejects a live futures host with cross-allowlist exception."""
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/ping")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_live_spot_host() -> None:
    """Per-request hook rejects a live spot host with cross-allowlist exception."""
    request = httpx.Request("GET", "https://api.binance.com/api/v3/account")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_spot_demo_host() -> None:
    """Per-request hook rejects Spot Demo host (cross-demo-lane leak)."""
    request = httpx.Request("GET", "https://demo-api.binance.com/api/v3/account")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_deprecated_futures_testnet_host() -> None:
    """Per-request hook rejects deprecated futures testnet host."""
    request = httpx.Request("GET", "https://testnet.binancefuture.com/fapi/v1/ping")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_deprecated_spot_testnet_host() -> None:
    """Per-request hook rejects deprecated spot testnet host."""
    request = httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_rejects_unknown_host() -> None:
    """Per-request hook rejects an arbitrary host with BinanceLiveHostBlocked."""
    request = httpx.Request("GET", "https://evil.example.com/fapi/v1/ping")
    with pytest.raises(BinanceLiveHostBlocked):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_accepts_futures_demo_host() -> None:
    """Per-request hook accepts the documented Futures Demo host."""
    request = httpx.Request("GET", "https://demo-fapi.binance.com/fapi/v1/ping")
    await _on_request(request)  # Should not raise.


@pytest.mark.asyncio
async def test_on_response_rejects_3xx_redirect() -> None:
    """3xx redirects refused — Futures Demo endpoints do not legitimately redirect."""
    request = httpx.Request("GET", "https://demo-fapi.binance.com/fapi/v1/ping")
    response = httpx.Response(
        302,
        headers={"location": "https://fapi.binance.com/fapi/v1/ping"},
        request=request,
    )
    with pytest.raises(BinanceLiveHostBlocked):
        await _on_response(response)


@pytest.mark.asyncio
async def test_on_response_accepts_2xx() -> None:
    """2xx responses pass through the post-response hook untouched."""
    request = httpx.Request("GET", "https://demo-fapi.binance.com/fapi/v1/ping")
    response = httpx.Response(200, request=request)
    await _on_response(response)  # Should not raise.
