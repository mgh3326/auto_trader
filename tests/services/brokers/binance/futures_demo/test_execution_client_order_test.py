"""ROB-298 PR 2 — ``order_test`` hits /fapi/v1/order/test, not /fapi/v1/order.

Binance Futures ``/fapi/v1/order/test`` validates the order shape without
placing it. The execution client exposes ``order_test`` as a distinct
method so callers cannot accidentally mix it with the real
``/fapi/v1/order`` POST.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.futures_demo.dto import FuturesDemoOrderTestResult
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
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
async def test_order_test_hits_order_test_path_not_order(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``order_test`` dispatches POST to ``/fapi/v1/order/test`` (empty 200 body)."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order/test\?.*$"),
        status_code=200,
        json={},
    )
    result = await client.order_test(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
    )
    assert isinstance(result, FuturesDemoOrderTestResult)
    assert result.symbol == "XRPUSDT"
    assert result.side == "BUY"
    assert result.order_type == "MARKET"
    assert result.qty == Decimal("10")

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert "/fapi/v1/order/test" in str(requests[0].url)
    # The naked /fapi/v1/order endpoint must NOT have been hit.
    assert not any(
        re.search(r"/fapi/v1/order(?!/test)", str(r.url)) for r in requests
    ), [str(r.url) for r in requests]


@pytest.mark.asyncio
async def test_order_test_limit_includes_price_and_tif(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``order_test`` for LIMIT passes ``price`` and ``timeInForce`` through."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order/test\?.*$"),
        status_code=200,
        json={},
    )
    result = await client.order_test(
        symbol="XRPUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("10"),
        price=Decimal("0.50"),
        time_in_force="GTC",
    )
    assert isinstance(result, FuturesDemoOrderTestResult)
    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url)
    assert "/fapi/v1/order/test" in url_str
    assert "price=0.50" in url_str
    assert "timeInForce=GTC" in url_str
    assert "type=LIMIT" in url_str
    assert "signature=" in url_str


@pytest.mark.asyncio
async def test_order_test_signs_with_apikey_header(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``order_test`` carries X-MBX-APIKEY + signature param like any signed call."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order/test\?.*$"),
        status_code=200,
        json={},
    )
    await client.order_test(
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
    )
    last = httpx_mock.get_request()
    assert last is not None
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_FUTURES_DEMO_KEY"
    assert "signature=" in str(last.url)
    assert "timestamp=" in str(last.url)
