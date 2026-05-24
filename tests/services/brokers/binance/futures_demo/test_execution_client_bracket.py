"""ROB-307 PR3 — futures reduceOnly trigger orders (STOP_MARKET / TAKE_PROFIT_MARKET).

Broker-side bracket exit legs: a signed POST /fapi/v1/order carrying
``type=STOP_MARKET`` or ``TAKE_PROFIT_MARKET``, ``stopPrice``, and
``reduceOnly=true`` (always — these only ever close, never open). Default
is a dry-run with zero HTTP. Demo host only; network mocked.
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
async def test_stop_market_dispatches_signed_reduce_only_trigger(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "clientOrderId": "sl-1",
            "orderId": 999,
            "symbol": "XRPUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "origQty": "7.3",
            "executedQty": "0",
            "status": "NEW",
            "reduceOnly": True,
        },
    )
    result = await client.submit_reduce_only_trigger(
        symbol="XRPUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        qty=Decimal("7.3"),
        stop_price=Decimal("1.33"),
        client_order_id="sl-1",
        confirm=True,
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    assert result.status == "NEW"
    assert result.reduce_only is True
    url = str(httpx_mock.get_requests()[0].url)
    assert "type=STOP_MARKET" in url
    assert "stopPrice=1.33" in url
    assert "reduceOnly=true" in url
    assert "price=" not in url  # MARKET-triggered: no limit price


@pytest.mark.asyncio
async def test_take_profit_market_dispatches_signed_trigger(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        json={
            "clientOrderId": "tp-1",
            "orderId": 1000,
            "symbol": "XRPUSDT",
            "side": "SELL",
            "type": "TAKE_PROFIT_MARKET",
            "origQty": "7.3",
            "executedQty": "0",
            "status": "NEW",
            "reduceOnly": True,
        },
    )
    result = await client.submit_reduce_only_trigger(
        symbol="XRPUSDT",
        side="SELL",
        order_type="TAKE_PROFIT_MARKET",
        qty=Decimal("7.3"),
        stop_price=Decimal("1.40"),
        client_order_id="tp-1",
        confirm=True,
    )
    assert isinstance(result, FuturesDemoOrderSubmitResult)
    url = str(httpx_mock.get_requests()[0].url)
    assert "type=TAKE_PROFIT_MARKET" in url
    assert "stopPrice=1.40" in url
    assert "reduceOnly=true" in url


@pytest.mark.asyncio
async def test_trigger_dry_run_dispatches_zero_http(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    result = await client.submit_reduce_only_trigger(
        symbol="XRPUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        qty=Decimal("7.3"),
        stop_price=Decimal("1.33"),
    )
    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.reduce_only is True
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_trigger_rejects_non_trigger_order_type(
    client: BinanceFuturesDemoExecutionClient,
) -> None:
    with pytest.raises(ValueError):
        await client.submit_reduce_only_trigger(
            symbol="XRPUSDT",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("7.3"),
            stop_price=Decimal("1.33"),
            confirm=True,
        )


@pytest.mark.asyncio
async def test_trigger_rejects_nonpositive_stop_price(
    client: BinanceFuturesDemoExecutionClient,
) -> None:
    with pytest.raises(ValueError):
        await client.submit_reduce_only_trigger(
            symbol="XRPUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            qty=Decimal("7.3"),
            stop_price=Decimal("0"),
            confirm=True,
        )
