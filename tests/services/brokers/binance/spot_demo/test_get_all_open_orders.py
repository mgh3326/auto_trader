"""Account-wide open-orders read (symbol omitted).

Mirrors the ROB-993 futures precedent: ``get_open_orders(symbol=...)`` cannot
answer "is anything else resting on this shared account?", so the additive
symbol-less read exists alongside it. The existing symbol-scoped method must
stay byte-identical.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

_BASE = "https://demo-api.binance.com"
_OPEN_ORDERS_RE = re.compile(r"^https://demo-api\.binance\.com/api/v3/openOrders\?.*$")

_ACCOUNT_WIDE_JSON = [
    {
        "symbol": "BTCUSDT",
        "orderId": 54962605151,
        "clientOrderId": "b0xc-40f2525f66712ec0",
        "side": "BUY",
        "origQty": "0.00015000",
        "status": "NEW",
    },
    {
        "symbol": "XRPUSDT",
        "orderId": 777,
        "clientOrderId": "someone-else-1",
        "side": "SELL",
        "origQty": "10.00000000",
        "status": "NEW",
    },
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceSpotDemoExecutionClient:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _BASE)
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_returns_orders_across_every_symbol(client, httpx_mock):
    httpx_mock.add_response(
        method="GET", url=_OPEN_ORDERS_RE, json=_ACCOUNT_WIDE_JSON
    )

    result = await client.get_all_open_orders()

    assert [o.symbol for o in result.orders] == ["BTCUSDT", "XRPUSDT"]
    assert result.orders[0].client_order_id == "b0xc-40f2525f66712ec0"
    assert result.orders[0].broker_order_id == "54962605151"
    assert result.orders[0].qty == Decimal("0.00015000")
    assert result.orders[1].side == "SELL"


@pytest.mark.asyncio
async def test_request_omits_symbol_so_binance_returns_the_whole_account(
    client, httpx_mock
):
    """A ``symbol`` param would silently narrow the answer to one market."""

    httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])

    await client.get_all_open_orders()

    request = httpx_mock.get_requests()[0]
    assert "symbol=" not in str(request.url)
    assert str(request.url).startswith(f"{_BASE}/api/v3/openOrders")


@pytest.mark.asyncio
async def test_empty_account_returns_no_orders(client, httpx_mock):
    httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])

    result = await client.get_all_open_orders()

    assert result.orders == []


@pytest.mark.asyncio
async def test_symbol_scoped_read_still_sends_its_symbol(client, httpx_mock):
    """The additive method must not have changed the existing one."""

    httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])

    await client.get_open_orders(symbol="BTCUSDT")

    assert "symbol=BTCUSDT" in str(httpx_mock.get_requests()[0].url)


@pytest.mark.asyncio
async def test_read_is_signed_and_stays_on_the_demo_host(client, httpx_mock):
    httpx_mock.add_response(method="GET", url=_OPEN_ORDERS_RE, json=[])

    await client.get_all_open_orders()

    url = str(httpx_mock.get_requests()[0].url)
    assert url.startswith("https://demo-api.binance.com/")
    assert "signature=" in url
    assert "timestamp=" in url
