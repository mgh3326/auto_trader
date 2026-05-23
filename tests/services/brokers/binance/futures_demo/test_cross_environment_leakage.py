"""ROB-298 PR 2 — Futures Demo transport runtime cross-allowlist guards.

The factory in test_transport.py covers base-URL rejection at construction.
This file verifies the on-request event hook also rejects runtime URL
redirects to cross-lane hosts after the client is constructed.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoCrossAllowlistViolation,
)
from app.services.brokers.binance.futures_demo.transport import (
    build_futures_demo_client,
)


@pytest.mark.asyncio
async def test_rejects_runtime_get_to_live_futures(httpx_mock) -> None:
    """Client rejects runtime attempt to GET from live Futures host."""
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    try:
        with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
            await client.get("https://fapi.binance.com/fapi/v1/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rejects_runtime_get_to_live_spot(httpx_mock) -> None:
    """Client rejects runtime attempt to GET from live Spot host."""
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    try:
        with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
            await client.get("https://api.binance.com/api/v3/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rejects_runtime_get_to_spot_demo(httpx_mock) -> None:
    """Client rejects runtime attempt to GET from Spot Demo host."""
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    try:
        with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
            await client.get("https://demo-api.binance.com/api/v3/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rejects_runtime_get_to_deprecated_futures_testnet(
    httpx_mock,
) -> None:
    """Client rejects runtime attempt to GET from deprecated Futures Testnet."""
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    try:
        with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
            await client.get("https://testnet.binancefuture.com/fapi/v1/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rejects_runtime_get_to_deprecated_spot_testnet(
    httpx_mock,
) -> None:
    """Client rejects runtime attempt to GET from deprecated Spot Testnet."""
    client = build_futures_demo_client(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    try:
        with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
            await client.get("https://testnet.binance.vision/api/v3/ping")
    finally:
        await client.aclose()
