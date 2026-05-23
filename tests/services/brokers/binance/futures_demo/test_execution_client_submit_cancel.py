"""ROB-298 PR 2 — Futures Demo submit_order/cancel_order operator gate + signed HTTP.

Mirrors the Spot Demo submit/cancel matrix but enforces the Futures Demo
endpoints (``/fapi/v1/order``, ``/fapi/v1/openOrders``):

  * ``submit_order(..., confirm=False)`` (the default) returns a
    ``FuturesDemoDryRunResult`` and dispatches zero HTTP.
  * ``submit_order(..., confirm=True)`` routes through the HMAC signing
    chokepoint and hits ``demo-fapi.binance.com/fapi/v1/order``.
  * ``cancel_order`` follows the same operator gate.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoCancelResult,
    FuturesDemoOpenOrdersResult,
    FuturesDemoOrderSubmitResult,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
    FuturesDemoDryRunResult,
)

_FUTURES_DEMO_BASE = "https://demo-fapi.binance.com"


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env to the enabled-with-credentials baseline used by all tests."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "DUMMY_FUTURES_DEMO_KEY")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "DUMMY_FUTURES_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", _FUTURES_DEMO_BASE)


@pytest.fixture
def client(enabled_env: None) -> BinanceFuturesDemoExecutionClient:
    return BinanceFuturesDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_preview_submit_returns_dry_run_zero_http(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``preview_submit`` is sync and dispatches zero HTTP."""
    result = client.preview_submit(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="preview-cid",
    )
    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.symbol == "XRPUSDT"
    assert result.side == "BUY"
    assert result.order_type == "MARKET"
    assert result.client_order_id == "preview-cid"
    assert result.reduce_only is False
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_submit_order_default_returns_dry_run(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Default (``confirm=False``) returns DryRun; zero HTTP."""
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="test-cid-default",
    )
    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.symbol == "XRPUSDT"
    assert result.side == "BUY"
    assert result.order_type == "MARKET"
    assert result.client_order_id == "test-cid-default"
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_submit_order_confirm_true_hits_futures_demo_host(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``confirm=True`` routes signed POST to demo-fapi.binance.com/fapi/v1/order."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "symbol": "XRPUSDT",
            "orderId": 12345,
            "clientOrderId": "test-cid-confirmed",
            "transactTime": 1700000000000,
            "price": "0.00",
            "avgPrice": "0.50",
            "origQty": "10",
            "executedQty": "10",
            "cumQuote": "5.00",
            "status": "FILLED",
            "timeInForce": "GTC",
            "type": "MARKET",
            "side": "BUY",
            "reduceOnly": False,
        },
        status_code=200,
    )
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="test-cid-confirmed",
        confirm=True,
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    assert result.broker_order_id == "12345"
    assert result.status == "FILLED"
    assert result.symbol == "XRPUSDT"
    assert result.client_order_id == "test-cid-confirmed"
    assert result.executed_qty == Decimal("10")
    assert result.reduce_only is False

    last = httpx_mock.get_request()
    assert last is not None
    assert last.url.host == "demo-fapi.binance.com"
    assert last.url.path == "/fapi/v1/order"
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_FUTURES_DEMO_KEY"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "timestamp=" in url_str


@pytest.mark.asyncio
async def test_submit_order_limit_includes_price_and_tif(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """LIMIT orders pass ``price`` and ``timeInForce`` through the signer."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "symbol": "XRPUSDT",
            "orderId": 67890,
            "clientOrderId": "test-limit-1",
            "transactTime": 1700000000000,
            "price": "0.50",
            "avgPrice": "0",
            "origQty": "10",
            "executedQty": "0",
            "cumQuote": "0",
            "status": "NEW",
            "timeInForce": "GTC",
            "type": "LIMIT",
            "side": "BUY",
            "reduceOnly": False,
        },
        status_code=200,
    )
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("10"),
        client_order_id="test-limit-1",
        confirm=True,
        price=Decimal("0.50"),
        time_in_force="GTC",
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    assert result.status == "NEW"
    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url)
    assert "price=0.50" in url_str
    assert "timeInForce=GTC" in url_str
    assert "type=LIMIT" in url_str


@pytest.mark.asyncio
async def test_cancel_order_hits_demo_futures_host(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``cancel_order`` signs and DELETEs /fapi/v1/order."""
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "symbol": "XRPUSDT",
            "orderId": 12345,
            "origClientOrderId": "test-cid-cancel-confirmed",
            "clientOrderId": "test-cid-cancel-confirmed",
            "status": "CANCELED",
        },
        status_code=200,
    )
    result = await client.cancel_order(
        symbol="XRPUSDT",
        client_order_id="test-cid-cancel-confirmed",
    )
    assert isinstance(result, FuturesDemoCancelResult)
    assert result.status == "CANCELED"
    assert result.broker_order_id == "12345"
    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "DELETE"
    assert last.url.host == "demo-fapi.binance.com"
    assert last.url.path == "/fapi/v1/order"
    assert "signature=" in str(last.url)


@pytest.mark.asyncio
async def test_get_open_orders_signs_and_hits_demo_futures_host(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Read-side open-orders query also signs and hits the Futures Demo host."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/openOrders\?.*$"),
        json=[
            {
                "symbol": "XRPUSDT",
                "orderId": 999,
                "clientOrderId": "cid-A",
                "side": "BUY",
                "type": "LIMIT",
                "origQty": "10",
                "price": "0.50",
                "status": "NEW",
                "reduceOnly": False,
                "updateTime": 1700000000000,
            }
        ],
        status_code=200,
    )
    result = await client.get_open_orders(symbol="XRPUSDT")
    assert isinstance(result, FuturesDemoOpenOrdersResult)
    assert len(result.orders) == 1
    assert result.orders[0].client_order_id == "cid-A"
    assert result.orders[0].broker_order_id == "999"
    assert result.orders[0].reduce_only is False
    last = httpx_mock.get_request()
    assert last is not None
    assert "signature=" in str(last.url)
    assert "timestamp=" in str(last.url)


@pytest.mark.asyncio
async def test_submit_order_rejects_zero_qty_before_signing(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``submit_order(qty=0, confirm=True)`` raises ValueError; zero HTTP."""
    with pytest.raises(ValueError, match=r"qty must be > 0"):
        await client.submit_order(
            symbol="XRPUSDT",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0"),
            client_order_id="zero-qty-cid",
            confirm=True,
        )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_submit_order_rejects_negative_qty(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``submit_order(qty=-0.001, confirm=True)`` raises ValueError; zero HTTP."""
    with pytest.raises(ValueError, match=r"qty must be > 0"):
        await client.submit_order(
            symbol="XRPUSDT",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("-0.001"),
            client_order_id="neg-qty-cid",
            confirm=True,
        )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_submit_order_rejects_empty_symbol(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``submit_order(symbol="", confirm=True)`` raises ValueError; zero HTTP."""
    with pytest.raises(ValueError, match=r"symbol must be non-empty"):
        await client.submit_order(
            symbol="",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("10"),
            client_order_id="empty-symbol-cid",
            confirm=True,
        )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_order_rejects_empty_client_order_id(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``cancel_order(client_order_id="")`` raises ValueError; zero HTTP."""
    with pytest.raises(ValueError, match=r"client_order_id must be non-empty"):
        await client.cancel_order(symbol="XRPUSDT", client_order_id="")
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_cancel_order_rejects_empty_symbol(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``cancel_order(symbol="")`` raises ValueError; zero HTTP."""
    with pytest.raises(ValueError, match=r"symbol must be non-empty"):
        await client.cancel_order(symbol="", client_order_id="some-cid")
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_preview_submit_rejects_zero_qty(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``preview_submit(qty=0)`` raises ValueError; zero HTTP (no signing either)."""
    with pytest.raises(ValueError, match=r"qty must be > 0"):
        client.preview_submit(
            symbol="XRPUSDT",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0"),
            client_order_id="preview-zero-qty",
        )
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_secret_not_in_logs_on_submit_failure(
    enabled_env: None, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Reviewer focus — secret never appears in logs on a 4xx broker reject."""
    import logging

    import httpx

    secret_str = "PLACEHOLDER_FUTURES_DEMO_SECRET_LEAK_PROBE"
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", secret_str)
    client = BinanceFuturesDemoExecutionClient.from_env()

    from unittest.mock import AsyncMock

    request = httpx.Request("POST", "https://demo-fapi.binance.com/fapi/v1/order")
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
                symbol="XRPUSDT",
                side="BUY",
                order_type="MARKET",
                qty=Decimal("10"),
                client_order_id="leak-probe-cid",
                confirm=True,
            )
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_str not in full_log, f"Secret leaked into log output: {full_log!r}"
