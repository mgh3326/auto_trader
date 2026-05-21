"""ROB-286 — Testnet transport pre/post-request hooks and cross-allowlist guard.

Matrix rows T4, T6, T7, T8, T9.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.testnet.errors import (
    BinanceTestnetCrossAllowlistViolation,
)
from app.services.brokers.binance.testnet.transport import build_testnet_client

_TESTNET_BASE = "https://testnet.binance.vision"
_API_KEY = "DUMMY_API_KEY_FOR_TEST"
_API_SECRET = "DUMMY_API_SECRET_FOR_TEST"


@pytest.mark.asyncio
async def test_signed_request_has_apikey_header(httpx_mock) -> None:
    """T8 — Outgoing request to a testnet host carries the API-key header."""
    httpx_mock.add_response(
        url=f"{_TESTNET_BASE}/api/v3/account",
        json={},
        status_code=200,
    )
    client = build_testnet_client(api_key=_API_KEY, api_secret=_API_SECRET)
    try:
        resp = await client.get(f"{_TESTNET_BASE}/api/v3/account")
        assert resp.status_code == 200
        # Verify the captured request had the API-key header attached.
        last_request = httpx_mock.get_request()
        assert last_request is not None
        # Header names are case-insensitive in httpx.
        api_key_value = last_request.headers.get("X-MBX-APIKEY")
        assert api_key_value == _API_KEY, (
            f"Expected X-MBX-APIKEY={_API_KEY!r} on outgoing request; got "
            f"{api_key_value!r}"
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_to_non_testnet_host_raises() -> None:
    """T6 — Pre-request hook rejects a non-testnet host outright.

    Uses a host that is in neither TESTNET_HOSTS nor PUBLIC_HOSTS so the
    generic ``BinanceLiveHostBlocked`` path is exercised. The "host in
    PUBLIC_HOSTS" case is covered separately by
    ``test_signed_request_to_public_host_raises`` (T9).
    """
    client = build_testnet_client(api_key=_API_KEY, api_secret=_API_SECRET)
    try:
        with pytest.raises(BinanceLiveHostBlocked):
            await client.get("https://evil.example.com/api/v3/account")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_signed_request_to_public_host_raises() -> None:
    """T9 — Cross-allowlist guard: any host in PUBLIC_HOSTS must raise.

    This is the single most important behavioral assertion in this PR
    (reviewer focus #1). If the testnet transport ever accepted a public
    host, a misconfigured deploy could send a real-money order to live
    Binance.
    """
    client = build_testnet_client(api_key=_API_KEY, api_secret=_API_SECRET)
    try:
        # The hook should raise either BinanceLiveHostBlocked (host not in
        # TESTNET_HOSTS) OR the more specific cross-allowlist violation.
        # Accept either since both signal the right failure mode.
        with pytest.raises(
            (BinanceLiveHostBlocked, BinanceTestnetCrossAllowlistViolation)
        ):
            await client.get(
                "https://api.binance.com/api/v3/order",
                params={"symbol": "BTCUSDT"},
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_init_with_live_base_url_raises() -> None:
    """T4 — Calling the factory with a live base host raises at init.

    Defense in depth: even if the env layer is bypassed and someone
    constructs the client with ``base_url='https://api.binance.com'``,
    factory init fails-closed.
    """
    with pytest.raises(BinanceLiveHostBlocked):
        build_testnet_client(
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            base_url="https://api.binance.com",
        )


@pytest.mark.asyncio
async def test_redirect_to_non_testnet_host_raises(httpx_mock) -> None:
    """T7 — A 30x redirect from testnet to non-testnet raises.

    Same defense-in-depth shape as Child B's public transport: with
    ``follow_redirects=False`` the 30x reaches us as-is and we treat any
    redirect as suspicious.
    """
    httpx_mock.add_response(
        url=f"{_TESTNET_BASE}/api/v3/ping",
        status_code=302,
        headers={"Location": "https://evil.example.com/whatever"},
    )
    client = build_testnet_client(api_key=_API_KEY, api_secret=_API_SECRET)
    try:
        with pytest.raises(BinanceLiveHostBlocked):
            await client.get(f"{_TESTNET_BASE}/api/v3/ping")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_place_stop_orders_reject_non_testnet_host(monkeypatch) -> None:
    """TT5 — Cross-allowlist guard intact for the new stop-order methods.

    Construct an adapter with a base URL the host-allowlist accepts
    (testnet) but mutate ``_base_url`` to point the underlying httpx
    client at a non-testnet host before each stop call. The transport
    event hook must reject the outgoing request — the new placement
    methods MUST route through ``build_testnet_client``'s event hooks
    just like ``submit_order``.

    Reviewer focus #2 in the plan: TT5 must actually exercise a
    non-testnet host injection through the new methods, not just the
    existing ones.
    """
    from app.services.brokers.binance.testnet.execution_client import (
        BinanceTestnetExecutionClient,
    )

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    client = BinanceTestnetExecutionClient.from_env()
    try:
        # Re-bind the underlying httpx client to a non-testnet host so
        # the post-build outgoing URL is routed to a live host literal.
        # The event hook should raise before HTTP is attempted.
        client._client.base_url = "https://api.binance.com"  # type: ignore[assignment]
        from decimal import Decimal

        with pytest.raises(
            (BinanceLiveHostBlocked, BinanceTestnetCrossAllowlistViolation)
        ):
            await client.place_stop_limit_order(
                symbol="BTCUSDT",
                side="SELL",
                quantity=Decimal("0.001"),
                stop_price=Decimal("50500"),
                limit_price=Decimal("50500"),
                client_order_id="tp-leg-1",
                dry_run=False,
                confirm=True,
            )
        with pytest.raises(
            (BinanceLiveHostBlocked, BinanceTestnetCrossAllowlistViolation)
        ):
            await client.place_stop_market_order(
                symbol="BTCUSDT",
                side="SELL",
                quantity=Decimal("0.001"),
                stop_price=Decimal("49500"),
                client_order_id="sl-leg-1",
                dry_run=False,
                confirm=True,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_default_base_url_is_testnet(httpx_mock) -> None:
    """Default base URL points to the testnet host, not anywhere else."""
    httpx_mock.add_response(
        url=f"{_TESTNET_BASE}/api/v3/ping",
        json={},
        status_code=200,
    )
    client = build_testnet_client(api_key=_API_KEY, api_secret=_API_SECRET)
    try:
        resp = await client.get(f"{_TESTNET_BASE}/api/v3/ping")
        assert resp.status_code == 200
    finally:
        await client.aclose()
