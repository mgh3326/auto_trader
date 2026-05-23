"""ROB-298 PR 2 — reduceOnly is correctly threaded through submit_order.

The ``reduce_only`` keyword on ``submit_order`` MUST be sent to Binance
as ``reduceOnly=true`` on close-side orders. This is the structural guard
against accidentally flipping a position (opening opposite side instead
of closing).

When ``reduce_only=False`` (default), the param may be omitted or sent as
``reduceOnly=false`` — both are acceptable to Binance, but the open-side
intent must NEVER appear as ``reduceOnly=true``.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderSubmitResult,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
    FuturesDemoDryRunResult,
)

_FUTURES_DEMO_BASE = "https://demo-fapi.binance.com"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceFuturesDemoExecutionClient:
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "DUMMY_FUTURES_DEMO_KEY")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "DUMMY_FUTURES_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", _FUTURES_DEMO_BASE)
    return BinanceFuturesDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_submit_order_with_reduce_only_true_sends_param(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``reduce_only=True`` → ``reduceOnly=true`` in signed request params."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "symbol": "XRPUSDT",
            "orderId": 42,
            "clientOrderId": "close-cid",
            "transactTime": 1700000000000,
            "price": "0",
            "avgPrice": "0.50",
            "origQty": "10",
            "executedQty": "10",
            "cumQuote": "5.00",
            "status": "FILLED",
            "timeInForce": "GTC",
            "type": "MARKET",
            "side": "SELL",
            "reduceOnly": True,
        },
        status_code=200,
    )
    result = await client.submit_order(
        symbol="XRPUSDT",
        side="SELL",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="close-cid",
        confirm=True,
        reduce_only=True,
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    assert result.reduce_only is True

    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url)
    # reduceOnly=true MUST appear in the signed payload (case-insensitive match
    # since some httpx normalizations lowercase the path/query but the structural
    # invariant is the param value).
    assert "reduceonly=true" in url_str.lower()


@pytest.mark.asyncio
async def test_submit_order_with_reduce_only_false_does_not_send_true(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``reduce_only=False`` (default) → no ``reduceOnly=true`` in request.

    Either omitted or sent as ``reduceOnly=false``; the structural
    invariant is that an open-side order never accidentally carries
    ``reduceOnly=true``.
    """
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "symbol": "XRPUSDT",
            "orderId": 43,
            "clientOrderId": "open-cid",
            "transactTime": 1700000000000,
            "price": "0",
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
    await client.submit_order(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="open-cid",
        confirm=True,
        # reduce_only intentionally unset — default is False
    )
    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url).lower()
    assert "reduceonly=true" not in url_str


@pytest.mark.asyncio
async def test_preview_submit_carries_reduce_only(
    client: BinanceFuturesDemoExecutionClient,
) -> None:
    """``preview_submit(reduce_only=True)`` carries the flag into the DryRun result."""
    result = client.preview_submit(
        symbol="XRPUSDT",
        side="SELL",
        order_type="MARKET",
        qty=Decimal("10"),
        client_order_id="preview-close-cid",
        reduce_only=True,
    )
    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.reduce_only is True
