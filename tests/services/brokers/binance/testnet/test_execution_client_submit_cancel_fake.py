"""ROB-286 — Confirmed submit/cancel exercised against fake testnet host.

Matrix rows T14, T15.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.testnet.dto import (
    CancelResult,
    OrderSubmitResult,
)
from app.services.brokers.binance.testnet.execution_client import (
    BinanceTestnetExecutionClient,
)

_TESTNET_BASE = "https://testnet.binance.vision"


@pytest.fixture
def client(monkeypatch) -> BinanceTestnetExecutionClient:
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    return BinanceTestnetExecutionClient.from_env()


@pytest.mark.asyncio
async def test_confirmed_submit_hits_testnet_host(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """T14 — submit_order(confirm=True, dry_run=False) hits testnet host."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 999,
            "orderListId": -1,
            "clientOrderId": "test-client-id",
            "transactTime": 1700000000000,
            "price": "50000.00",
            "origQty": "0.001",
            "executedQty": "0.000",
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
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        notional_usdt=Decimal("5"),
        client_order_id="test-client-id",
        dry_run=False,
        confirm=True,
    )
    assert isinstance(result, OrderSubmitResult)
    assert result.broker_order_id == "999"
    assert result.status == "NEW"
    assert result.symbol == "BTCUSDT"
    assert result.client_order_id == "test-client-id"

    # Verify the request hit the testnet host with the X-MBX-APIKEY header
    # and a signed payload (contains a signature param).
    last = httpx_mock.get_request()
    assert last is not None
    assert last.url.host == "testnet.binance.vision"
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_KEY"
    # Query string carries the signature parameter.
    assert "signature=" in str(last.url)
    assert "timestamp=" in str(last.url)


@pytest.mark.asyncio
async def test_confirmed_cancel_hits_testnet_host(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """T15 — cancel_order(confirm=True, dry_run=False) hits testnet host."""
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 999,
            "origClientOrderId": "test-client-id",
            "clientOrderId": "test-client-id",
            "status": "CANCELED",
        },
        status_code=200,
    )
    result = await client.cancel_order(
        symbol="BTCUSDT",
        client_order_id="test-client-id",
        dry_run=False,
        confirm=True,
    )
    assert isinstance(result, CancelResult)
    assert result.status == "CANCELED"
    assert result.broker_order_id == "999"
    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "DELETE"
    assert last.url.host == "testnet.binance.vision"
    assert "signature=" in str(last.url)


@pytest.mark.asyncio
async def test_confirmed_submit_with_dry_run_true_does_not_hit_http(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """confirm=True + dry_run=True (default for confirm-only paths) — no HTTP."""
    result = await client.submit_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        notional_usdt=Decimal("5"),
        dry_run=True,
        confirm=True,
    )
    # dry_run=True + confirm=True is treated as preview-only.
    from app.services.brokers.binance.testnet.dto import DryRunResult

    assert isinstance(result, DryRunResult)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_open_orders_query_signs_and_hits_testnet(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """Read-side queries also sign and hit testnet host (reconcile codepath)."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/openOrders\?.*$"),
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
    orders = await client.open_orders(symbol="BTCUSDT")
    assert isinstance(orders, list)
    assert len(orders) == 1
    last = httpx_mock.get_request()
    assert last is not None
    assert "signature=" in str(last.url)
    assert "timestamp=" in str(last.url)


@pytest.mark.asyncio
async def test_recent_fills_query_signs_and_hits_testnet(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """myTrades read query also signs."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/myTrades\?.*$"),
        json=[],
        status_code=200,
    )
    fills = await client.recent_fills(symbol="BTCUSDT", limit=50)
    assert fills == []
    last = httpx_mock.get_request()
    assert last is not None
    assert "signature=" in str(last.url)


# --------------------------------------------------------------------------
# ROB-289 — Paired TP/SL stop-order placement tests (TT1-TT4).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_stop_limit_order_dry_run_no_http(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """TT1 — place_stop_limit_order(confirm=False) returns DryRunResult, no HTTP.

    Default ``confirm=False`` means the operator gate is not passed and
    no signed request reaches the testnet. ``dry_run=True`` (default) is
    the safe path the smoke CLI exercises.
    """
    from app.services.brokers.binance.testnet.dto import DryRunResult

    result = await client.place_stop_limit_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.001"),
        stop_price=Decimal("50500"),
        limit_price=Decimal("50500"),
        client_order_id="tp-leg-1",
    )
    assert isinstance(result, DryRunResult)
    assert result.preview.client_order_id == "tp-leg-1"
    assert result.preview.signed_payload_template["type"] == "STOP_LOSS_LIMIT"
    assert result.preview.signed_payload_template["timeInForce"] == "GTC"
    # No HTTP attempted.
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_place_stop_limit_order_confirmed_hits_testnet_host(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """TT2 — confirm=True + dry_run=False routes through HMAC chokepoint.

    The signed POST hits ``testnet.binance.vision/api/v3/order`` with the
    X-MBX-APIKEY header and a signature param. The order type is locked
    to ``STOP_LOSS_LIMIT`` for the TP leg.
    """
    from app.services.brokers.binance.testnet.dto import StopOrderResult

    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 1234,
            "orderListId": -1,
            "clientOrderId": "tp-leg-1",
            "transactTime": 1700000000000,
            "price": "50500.00",
            "origQty": "0.001",
            "executedQty": "0.000",
            "cummulativeQuoteQty": "0.00",
            "status": "NEW",
            "timeInForce": "GTC",
            "type": "STOP_LOSS_LIMIT",
            "side": "SELL",
            "stopPrice": "50500.00",
        },
        status_code=200,
    )
    result = await client.place_stop_limit_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.001"),
        stop_price=Decimal("50500"),
        limit_price=Decimal("50500"),
        client_order_id="tp-leg-1",
        dry_run=False,
        confirm=True,
    )
    assert isinstance(result, StopOrderResult)
    assert result.broker_order_id == "1234"
    assert result.order_type == "STOP_LOSS_LIMIT"
    assert result.limit_price == Decimal("50500.00")
    assert result.stop_price == Decimal("50500.00")
    # Hit the testnet host with API-key header + signed query string.
    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "POST"
    assert last.url.host == "testnet.binance.vision"
    assert last.url.path == "/api/v3/order"
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_KEY"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "timestamp=" in url_str
    assert "type=STOP_LOSS_LIMIT" in url_str
    assert "timeInForce=GTC" in url_str
    assert "stopPrice=50500" in url_str


@pytest.mark.asyncio
async def test_place_stop_market_order_dry_run_no_http(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """TT3 — place_stop_market_order(confirm=False) returns DryRunResult, no HTTP."""
    from app.services.brokers.binance.testnet.dto import DryRunResult

    result = await client.place_stop_market_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.001"),
        stop_price=Decimal("49500"),
        client_order_id="sl-leg-1",
    )
    assert isinstance(result, DryRunResult)
    assert result.preview.client_order_id == "sl-leg-1"
    assert result.preview.signed_payload_template["type"] == "STOP_LOSS"
    # No timeInForce on stop-market.
    assert "timeInForce" not in result.preview.signed_payload_template
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_place_stop_market_order_confirmed_hits_testnet_host(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """TT4 — confirm=True + dry_run=False signs and POSTs ``STOP_LOSS``."""
    from app.services.brokers.binance.testnet.dto import StopOrderResult

    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://testnet\.binance\.vision/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 5678,
            "orderListId": -1,
            "clientOrderId": "sl-leg-1",
            "transactTime": 1700000000000,
            "price": "0.00000000",
            "origQty": "0.001",
            "status": "NEW",
            "type": "STOP_LOSS",
            "side": "SELL",
            "stopPrice": "49500.00",
        },
        status_code=200,
    )
    result = await client.place_stop_market_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.001"),
        stop_price=Decimal("49500"),
        client_order_id="sl-leg-1",
        dry_run=False,
        confirm=True,
    )
    assert isinstance(result, StopOrderResult)
    assert result.broker_order_id == "5678"
    assert result.order_type == "STOP_LOSS"
    # Stop-market has no limit_price.
    assert result.limit_price is None
    assert result.stop_price == Decimal("49500.00")
    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "POST"
    assert last.url.host == "testnet.binance.vision"
    assert last.url.path == "/api/v3/order"
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_KEY"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "type=STOP_LOSS" in url_str
    # STOP_LOSS (stop-market) must NOT carry timeInForce.
    assert "timeInForce" not in url_str
    # And must NOT carry a price param (it's stop-market).
    assert "&price=" not in url_str
