"""ROB-298 — ``order_test`` hits /api/v3/order/test, not /api/v3/order.

The Binance Spot ``/api/v3/order/test`` endpoint validates the order
shape without placing it; useful in CI/dry-run gates. The execution
client exposes ``order_test`` as a distinct method so callers cannot
accidentally mix it with the real ``/api/v3/order`` POST.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.dto import SpotDemoOrderTestResult
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

_SPOT_DEMO_BASE = "https://demo-api.binance.com"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceSpotDemoExecutionClient:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_SPOT_DEMO_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SPOT_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _SPOT_DEMO_BASE)
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_order_test_hits_order_test_path(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """``order_test`` dispatches POST to ``/api/v3/order/test`` (empty 200 body)."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order/test\?.*$"),
        status_code=200,
        json={},
    )
    result = await client.order_test(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
    )
    assert isinstance(result, SpotDemoOrderTestResult)
    assert result.symbol == "BTCUSDT"
    assert result.side == "BUY"
    assert result.order_type == "MARKET"
    assert result.qty == Decimal("0.0001")

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert "/api/v3/order/test" in str(requests[0].url)
    # The naked /api/v3/order endpoint must NOT have been hit.
    assert not any(
        re.search(r"/api/v3/order(?!/test)", str(r.url)) for r in requests
    ), [str(r.url) for r in requests]


@pytest.mark.asyncio
async def test_order_test_limit_includes_price_and_tif(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """``order_test`` for LIMIT passes ``price`` and ``timeInForce`` through."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order/test\?.*$"),
        status_code=200,
        json={},
    )
    result = await client.order_test(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.0001"),
        price=Decimal("50000"),
        time_in_force="GTC",
    )
    assert isinstance(result, SpotDemoOrderTestResult)
    last = httpx_mock.get_request()
    assert last is not None
    url_str = str(last.url)
    assert "/api/v3/order/test" in url_str
    assert "price=50000" in url_str
    assert "timeInForce=GTC" in url_str
    assert "type=LIMIT" in url_str
    assert "signature=" in url_str


@pytest.mark.asyncio
async def test_order_test_signs_with_apikey_header(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    """``order_test`` carries X-MBX-APIKEY + signature param like any signed call."""
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order/test\?.*$"),
        status_code=200,
        json={},
    )
    await client.order_test(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.0001"),
    )
    last = httpx_mock.get_request()
    assert last is not None
    assert last.headers.get("X-MBX-APIKEY") == "DUMMY_SPOT_DEMO_KEY"
    assert "signature=" in str(last.url)
    assert "timestamp=" in str(last.url)
