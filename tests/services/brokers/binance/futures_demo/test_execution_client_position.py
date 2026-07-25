"""ROB-303 — get_position() must hit /fapi/v2/positionRisk on demo-fapi.

demo-fapi rejects the legacy ``GET /fapi/v1/positionRisk`` endpoint with
``404 {"code": -5000, "msg": "Path /fapi/v1/positionRisk, Method GET is
invalid"}``. The confirm/reconcile path depends on ``get_position()`` to
verify a position before the reduceOnly close, so a 404 here aborts the
smoke after a real Demo position has been opened (leaving an anomaly).

These tests pin the position-reconcile source to the demo-fapi-supported
``/fapi/v2/positionRisk`` and guard against a regression back to v1.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoPositionResult,
)
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
async def test_get_position_hits_v2_position_risk_path(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``get_position`` dispatches GET /fapi/v2/positionRisk (not v1).

    Regression guard for ROB-303: v1 is rejected by demo-fapi (-5000).
    """
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v2/positionRisk\?.*$"),
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "7.4",
                "entryPrice": "0.5000",
                "leverage": "1",
            }
        ],
    )
    await client.get_position(symbol="XRPUSDT")

    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "GET"
    assert last.url.path == "/fapi/v2/positionRisk"
    assert last.url.path != "/fapi/v1/positionRisk"
    url_str = str(last.url)
    assert "signature=" in url_str
    assert "timestamp=" in url_str


@pytest.mark.asyncio
async def test_get_position_parses_v2_position_risk_row(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """v2 positionRisk list row parses into a signed FuturesDemoPositionResult."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v2/positionRisk\?.*$"),
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "7.4",
                "entryPrice": "0.5000",
                "leverage": "1",
            }
        ],
    )
    result = await client.get_position(symbol="XRPUSDT")
    assert isinstance(result, FuturesDemoPositionResult)
    assert result.symbol == "XRPUSDT"
    assert result.position_amt == Decimal("7.4")
    assert result.entry_price == Decimal("0.5000")
    assert result.leverage == 1
    assert result.is_flat is False


@pytest.mark.asyncio
async def test_get_position_flat_when_amount_zero(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Zero ``positionAmt`` from v2 positionRisk yields ``is_flat=True``."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v2/positionRisk\?.*$"),
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "0.0",
                "entryPrice": "0.0",
                "leverage": "1",
            }
        ],
    )
    result = await client.get_position(symbol="XRPUSDT")
    assert result.position_amt == Decimal("0.0")
    assert result.is_flat is True


@pytest.mark.asyncio
async def test_get_all_positions_omits_symbol_param_and_returns_every_row(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """ROB-993 (verify-993-r2-2329.md Finding 2) — account-wide position
    read used by the strategy loop's broker-flat gate. No ``symbol`` query
    param; every row Binance returns is surfaced, flat or not."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v2/positionRisk\?.*$"),
        status_code=200,
        json=[
            {
                "symbol": "XRPUSDT",
                "positionAmt": "0.0",
                "entryPrice": "0.0",
                "leverage": "1",
            },
            {
                "symbol": "DOGEUSDT",
                "positionAmt": "5.0",
                "entryPrice": "0.1",
                "leverage": "1",
            },
        ],
    )
    results = await client.get_all_positions()
    request = httpx_mock.get_requests()[0]
    assert "symbol=" not in str(request.url)

    assert [r.symbol for r in results] == ["XRPUSDT", "DOGEUSDT"]
    assert results[0].is_flat is True
    assert results[1].is_flat is False
    assert results[1].position_amt == Decimal("5.0")


@pytest.mark.asyncio
async def test_get_all_open_orders_omits_symbol_param_and_returns_every_row(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """ROB-993 (verify-993-r2-2329.md Finding 2) — account-wide open-order
    read used by the strategy loop's broker-flat gate."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/openOrders\?.*$"),
        status_code=200,
        json=[
            {
                "symbol": "DOGEUSDT",
                "orderId": 1,
                "clientOrderId": "stray-doge",
                "side": "SELL",
                "origQty": "1.0",
                "status": "NEW",
                "reduceOnly": False,
            }
        ],
    )
    result = await client.get_all_open_orders()
    request = httpx_mock.get_requests()[0]
    assert "symbol=" not in str(request.url)

    assert len(result.orders) == 1
    assert result.orders[0].symbol == "DOGEUSDT"
    assert result.orders[0].client_order_id == "stray-doge"
