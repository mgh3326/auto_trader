"""ROB-298 — submit_order/cancel_order operator gate + signed HTTP path.

Mirrors the testnet submit/cancel test matrix but enforces the stricter
Spot Demo invariants:

  * ``submit_order(..., confirm=False)`` (the default) returns a
    ``SpotDemoDryRunResult`` and dispatches zero HTTP.
  * ``submit_order(..., confirm=True)`` routes through the HMAC signing
    chokepoint and hits ``demo-api.binance.com/api/v3/order``.
  * ``cancel_order`` follows the same operator gate.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoCancelResult,
    SpotDemoOrderSubmitResult,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)

_SPOT_DEMO_BASE = "https://demo-api.binance.com"


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env to the enabled-with-credentials baseline used by all tests."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_SPOT_DEMO_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SPOT_DEMO_SECRET")
    # Default base_url; explicit so the test is self-contained.
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _SPOT_DEMO_BASE)


@pytest.fixture
def client(enabled_env: None) -> BinanceSpotDemoExecutionClient:
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_submit_order_default_returns_dry_run(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """Default (``confirm=False``) returns SpotDemoDryRunResult; zero HTTP."""
    result = await client.submit_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
        client_order_id="test-cid-default",
    )
    assert isinstance(result, SpotDemoDryRunResult)
    assert result.symbol == "BTCUSDT"
    assert result.side == "BUY"
    assert result.order_type == "MARKET"
    assert result.client_order_id == "test-cid-default"
    # No HTTP dispatched.
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_submit_order_confirm_true_hits_demo_host(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """``confirm=True`` routes signed POST to demo-api.binance.com/api/v3/order."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 12345,
            "orderListId": -1,
            "clientOrderId": "test-cid-confirmed",
            "transactTime": 1700000000000,
            "price": "0.00",
            "origQty": "0.0001",
            "executedQty": "0.0001",
            "cummulativeQuoteQty": "5.00",
            "status": "FILLED",
            "timeInForce": "GTC",
            "type": "MARKET",
            "side": "BUY",
            "fills": [
                {
                    "price": "50000",
                    "qty": "0.0001",
                    "commission": "0.000001",
                    "commissionAsset": "BTC",
                }
            ],
        },
        status_code=200,
    )
    result = await client.submit_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
        client_order_id="test-cid-confirmed",
        confirm=True,
    )
    assert isinstance(result, SpotDemoOrderSubmitResult)
    assert result.broker_order_id == "12345"
    assert result.status == "FILLED"
    assert result.symbol == "BTCUSDT"
    assert result.client_order_id == "test-cid-confirmed"
    assert result.executed_qty == Decimal("0.0001")
    assert result.cummulative_quote_qty == Decimal("5.00")
    assert result.fee_usdt == Decimal("0.050000")

    # Verify the request hit the Spot Demo host with the X-MBX-APIKEY header
    # and a signed payload (contains a signature param).
    last = httpx_mock.get_request()
    assert last is not None
    assert last.url.host == "demo-api.binance.com"
    assert last.url.path == "/api/v3/order"
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_SPOT_DEMO_KEY"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "timestamp=" in url_str


@pytest.mark.asyncio
async def test_submit_order_limit_includes_price_and_tif(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """LIMIT orders pass ``price`` and ``timeInForce`` through the signer."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 67890,
            "clientOrderId": "test-limit-1",
            "transactTime": 1700000000000,
            "price": "50000.00",
            "origQty": "0.0001",
            "executedQty": "0.0000",
            "cummulativeQuoteQty": "0.00",
            "status": "NEW",
            "timeInForce": "GTC",
            "type": "LIMIT",
            "side": "BUY",
        },
        status_code=200,
    )
    result = await client.submit_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.0001"),
        client_order_id="test-limit-1",
        confirm=True,
        price=Decimal("50000"),
        time_in_force="GTC",
    )
    assert isinstance(result, SpotDemoOrderSubmitResult)
    assert result.status == "NEW"
    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url)
    assert "price=50000" in url_str
    assert "timeInForce=GTC" in url_str
    assert "type=LIMIT" in url_str


@pytest.mark.asyncio
async def test_cancel_order_default_returns_dry_run(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """``cancel_order(..., confirm=False)`` returns DryRunResult; zero HTTP."""
    result = await client.cancel_order(
        symbol="BTCUSDT",
        client_order_id="test-cid-cancel",
    )
    assert isinstance(result, SpotDemoDryRunResult)
    assert result.client_order_id == "test-cid-cancel"
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_order_confirm_true_hits_demo_host(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """``cancel_order(..., confirm=True)`` signs and DELETEs /api/v3/order."""
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 12345,
            "origClientOrderId": "test-cid-cancel-confirmed",
            "clientOrderId": "test-cid-cancel-confirmed",
            "status": "CANCELED",
        },
        status_code=200,
    )
    result = await client.cancel_order(
        symbol="BTCUSDT",
        client_order_id="test-cid-cancel-confirmed",
        confirm=True,
    )
    assert isinstance(result, SpotDemoCancelResult)
    assert result.status == "CANCELED"
    assert result.broker_order_id == "12345"
    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "DELETE"
    assert last.url.host == "demo-api.binance.com"
    assert last.url.path == "/api/v3/order"
    assert "signature=" in str(last.url)


@pytest.mark.asyncio
async def test_get_open_orders_signs_and_hits_demo_host(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """Read-side open-orders query also signs and hits the Spot Demo host."""
    from app.services.brokers.binance.spot_demo.dto import SpotDemoOpenOrdersResult

    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/openOrders\?.*$"),
        json=[
            {
                "symbol": "BTCUSDT",
                "orderId": 999,
                "clientOrderId": "cid-A",
                "side": "BUY",
                "type": "LIMIT",
                "origQty": "0.001",
                "price": "50000",
                "status": "NEW",
                "updateTime": 1700000000000,
            }
        ],
        status_code=200,
    )
    result = await client.get_open_orders(symbol="BTCUSDT")
    assert isinstance(result, SpotDemoOpenOrdersResult)
    assert len(result.orders) == 1
    assert result.orders[0].client_order_id == "cid-A"
    assert result.orders[0].broker_order_id == "999"
    last = httpx_mock.get_request()
    assert last is not None
    assert "signature=" in str(last.url)
    assert "timestamp=" in str(last.url)


@pytest.mark.asyncio
async def test_secret_not_in_logs_on_submit_failure(
    enabled_env: None, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Reviewer focus — secret never appears in logs on a 4xx broker reject."""
    import logging

    import httpx

    secret_str = "PLACEHOLDER_SPOT_DEMO_SECRET_LEAK_PROBE"
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", secret_str)
    client = BinanceSpotDemoExecutionClient.from_env()

    from unittest.mock import AsyncMock

    request = httpx.Request("POST", "https://demo-api.binance.com/api/v3/order")
    response = httpx.Response(
        400, json={"code": -2010, "msg": "rejected"}, request=request
    )
    client._client.post = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "rejected", request=request, response=response
        )
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(httpx.HTTPStatusError):
            await client.submit_order(
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                qty=Decimal("0.0001"),
                client_order_id="leak-probe-cid",
                confirm=True,
            )
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_str not in full_log, f"Secret leaked into log output: {full_log!r}"
