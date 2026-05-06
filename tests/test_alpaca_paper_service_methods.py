"""Unit tests for AlpacaPaperBrokerService methods with mocked transport (ROB-57)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.brokers.alpaca.transport import HTTPTransport


def _make_response(data: Any, status_code: int = 200) -> httpx.Response:
    body = json.dumps(data).encode() if data is not None else b""
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
    )


def _make_service(transport: HTTPTransport) -> AlpacaPaperBrokerService:
    settings = AlpacaPaperSettings(
        api_key="pk-test",
        api_secret="sk-test",
        base_url=PAPER_TRADING_BASE_URL,
    )
    return AlpacaPaperBrokerService(transport=transport, settings=settings)


def _mock_transport(return_value: Any, status_code: int = 200) -> AsyncMock:
    transport = AsyncMock()
    transport.request = AsyncMock(
        return_value=_make_response(return_value, status_code)
    )
    return transport


ACCOUNT_DATA = {
    "id": "acct-001",
    "buying_power": "100000",
    "cash": "50000",
    "portfolio_value": "150000",
    "status": "ACTIVE",
}

POSITION_DATA = [
    {
        "asset_id": "asset-1",
        "symbol": "AAPL",
        "qty": "10",
        "avg_entry_price": "150.00",
        "current_price": "155.00",
        "market_value": "1550.00",
        "unrealized_pl": "50.00",
        "side": "long",
    },
    {
        "asset_id": "asset-2",
        "symbol": "TSLA",
        "qty": "5",
        "avg_entry_price": "200.00",
        "current_price": "210.00",
        "market_value": "1050.00",
        "unrealized_pl": "50.00",
        "side": "long",
    },
]

ASSET_DATA = [
    {
        "id": "asset-uuid-1",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "status": "active",
        "tradable": True,
        "class": "us_equity",
    }
]

ORDER_DATA = {
    "id": "order-001",
    "client_order_id": "client-001",
    "symbol": "AAPL",
    "qty": "10",
    "notional": None,
    "filled_qty": "0",
    "side": "buy",
    "type": "limit",
    "time_in_force": "day",
    "status": "new",
    "limit_price": "150.00",
    "stop_price": None,
    "filled_avg_price": None,
    "submitted_at": "2024-01-01T10:00:00Z",
    "filled_at": None,
}

FILL_DATA = [
    {
        "id": "fill-001",
        "activity_type": "FILL",
        "symbol": "AAPL",
        "qty": "10",
        "price": "150.50",
        "side": "buy",
        "transaction_time": "2024-01-01T10:01:00Z",
        "order_id": "order-001",
        "cum_qty": "10",
        "leaves_qty": "0",
        "order_status": "filled",
    }
]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_account_returns_snapshot():
    transport = _mock_transport(ACCOUNT_DATA)
    svc = _make_service(transport)

    account = await svc.get_account()

    assert account.id == "acct-001"
    assert account.buying_power == pytest.approx(Decimal("100000"))
    assert account.cash == pytest.approx(Decimal("50000"))
    assert account.portfolio_value == pytest.approx(Decimal("150000"))
    assert account.status == "ACTIVE"
    transport.request.assert_called_once_with("GET", "/v2/account")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_cash_returns_cash_balance():
    transport = _mock_transport(ACCOUNT_DATA)
    svc = _make_service(transport)

    cash = await svc.get_cash()

    assert cash.cash == pytest.approx(Decimal("50000"))
    assert cash.buying_power == pytest.approx(Decimal("100000"))
    transport.request.assert_called_once_with("GET", "/v2/account")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_positions_parses_array():
    transport = _mock_transport(POSITION_DATA)
    svc = _make_service(transport)

    positions = await svc.list_positions()

    assert len(positions) == 2
    assert positions[0].symbol == "AAPL"
    assert positions[1].symbol == "TSLA"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_positions_empty():
    transport = _mock_transport([])
    svc = _make_service(transport)

    positions = await svc.list_positions()

    assert positions == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_assets_passes_status_and_class_query():
    transport = _mock_transport(ASSET_DATA)
    svc = _make_service(transport)

    assets = await svc.list_assets(status="active", asset_class="us_equity")

    transport.request.assert_called_once_with(
        "GET",
        "/v2/assets",
        params={"status": "active", "asset_class": "us_equity"},
    )
    assert len(assets) == 1
    assert assets[0].symbol == "AAPL"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_order_marshals_request():
    transport = _mock_transport(ORDER_DATA)
    svc = _make_service(transport)

    order_request = OrderRequest(
        symbol="AAPL",
        qty=Decimal("10"),
        side="buy",
        type="limit",
        time_in_force="day",
        limit_price=Decimal("150.00"),
    )
    order = await svc.submit_order(order_request)

    assert transport.request.call_args[0][0] == "POST"
    assert transport.request.call_args[0][1] == "/v2/orders"
    body = transport.request.call_args[1]["json"]
    assert body["symbol"] == "AAPL"
    assert body["side"] == "buy"
    assert body["type"] == "limit"
    assert body["qty"] == "10"
    assert body["limit_price"] == "150.00"
    assert order.id == "order-001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_orders_with_status_filter():
    transport = _mock_transport([ORDER_DATA])
    svc = _make_service(transport)

    orders = await svc.list_orders(status="open", limit=50)

    transport.request.assert_called_once_with(
        "GET",
        "/v2/orders",
        params={"status": "open", "limit": 50},
    )
    assert len(orders) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_by_id():
    transport = _mock_transport(ORDER_DATA)
    svc = _make_service(transport)

    order = await svc.get_order("order-001")

    transport.request.assert_called_once_with("GET", "/v2/orders/order-001")
    assert order.id == "order-001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_order_returns_none_on_204():
    transport = AsyncMock()
    transport.request = AsyncMock(
        return_value=httpx.Response(status_code=204, content=b"")
    )
    svc = _make_service(transport)

    result = await svc.cancel_order("order-001")

    assert result is None
    transport.request.assert_called_once_with("DELETE", "/v2/orders/order-001")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_fills_uses_activities_executions_endpoint():
    transport = _mock_transport(FILL_DATA)
    svc = _make_service(transport)

    after = datetime(2024, 1, 1, tzinfo=UTC)
    until = datetime(2024, 1, 2, tzinfo=UTC)
    fills = await svc.list_fills(after=after, until=until, limit=100)

    call_args = transport.request.call_args
    assert call_args[0][0] == "GET"
    assert call_args[0][1] == "/v2/account/activities/FILL"
    params = call_args[1]["params"]
    assert "after" in params
    assert "until" in params
    assert params["limit"] == 100
    assert len(fills) == 1
    assert fills[0].symbol == "AAPL"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_error_wraps_http_error():
    transport = AsyncMock()
    transport.request = AsyncMock(
        return_value=httpx.Response(
            status_code=422,
            content=b'{"message": "unprocessable entity"}',
            headers={"content-type": "application/json"},
        )
    )
    svc = _make_service(transport)

    with pytest.raises(AlpacaPaperRequestError) as exc_info:
        await svc.get_account()

    assert exc_info.value.status_code == 422
