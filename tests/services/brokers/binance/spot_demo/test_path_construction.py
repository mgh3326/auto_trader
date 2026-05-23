"""ROB-296 — Path construction tests.

Verify that ``base_url + path`` composition yields the documented
``/api/v3/...`` endpoints without ``/api/api/v3`` duplication.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.spot_demo.transport import build_spot_demo_client


@pytest.mark.parametrize(
    "path",
    [
        "/api/v3/account",
        "/api/v3/ping",
        "/api/v3/order",
    ],
)
def test_path_is_not_duplicated(path: str) -> None:
    """httpx joins base_url + path without ``/api/api/v3`` duplication."""
    client = build_spot_demo_client(
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://demo-api.binance.com",
    )
    try:
        request = client.build_request("GET", path)
        assert str(request.url) == f"https://demo-api.binance.com{path}"
        assert "/api/api/v3" not in str(request.url)
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_base_url_with_trailing_slash_does_not_duplicate() -> None:
    """Trailing slash on base_url + leading slash on path → no duplication."""
    client = build_spot_demo_client(
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://demo-api.binance.com/",
    )
    try:
        request = client.build_request("GET", "/api/v3/account")
        assert str(request.url) == "https://demo-api.binance.com/api/v3/account"
        assert "/api/api/v3" not in str(request.url)
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_apikey_header_attached() -> None:
    """The ``X-MBX-APIKEY`` header is attached on every request."""
    client = build_spot_demo_client(
        api_key="my-key",
        api_secret="my-secret",
        base_url="https://demo-api.binance.com",
    )
    try:
        request = client.build_request("GET", "/api/v3/account")
        assert request.headers.get("X-MBX-APIKEY") == "my-key"
    finally:
        import asyncio

        asyncio.run(client.aclose())
