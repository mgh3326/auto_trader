"""ROB-285 — Transport-layer host allowlist + API-key rejection."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import (
    BinanceLiveHostBlocked,
    BinanceSignedEndpointAttempted,
)
from app.services.brokers.binance.transport import build_public_client


@pytest.mark.asyncio
async def test_get_to_allowed_host_is_passed_through(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/ping",
        json={},
        status_code=200,
    )
    client = build_public_client()
    try:
        resp = await client.get("https://api.binance.com/api/v3/ping")
        assert resp.status_code == 200
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_to_non_allowed_host_raises_at_request_time() -> None:
    client = build_public_client()
    try:
        with pytest.raises(BinanceLiveHostBlocked):
            await client.get("https://fapi.binance.com/fapi/v1/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_with_api_key_header_raises() -> None:
    client = build_public_client()
    try:
        with pytest.raises(BinanceSignedEndpointAttempted):
            await client.get(
                "https://api.binance.com/api/v3/account",
                headers={"X-MBX-APIKEY": "any-value"},
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_to_non_allowed_host_raises(httpx_mock) -> None:
    """Defense in depth: follow_redirects=False means a 30x response is
    surfaced; the response hook treats any 30x as suspicious because
    Binance public endpoints do not legitimately redirect."""
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/ping",
        status_code=302,
        headers={"Location": "https://evil.example.com/whatever"},
    )
    client = build_public_client()
    try:
        with pytest.raises(BinanceLiveHostBlocked):
            await client.get("https://api.binance.com/api/v3/ping")
    finally:
        await client.aclose()
