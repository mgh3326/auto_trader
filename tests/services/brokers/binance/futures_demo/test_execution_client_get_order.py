"""ROB-305 — get_order() single-order status query on demo-fapi.

Section 4 of ROB-305 requires bounded reconciliation of a submit response
that returns ``status=NEW``: the smoke must NOT treat ``NEW`` as a final
success/failure, and must NOT close a ``submitted`` ledger row. Instead it
polls the order's real status through signed reads. ``get_order`` is the
``GET /fapi/v1/order`` source for that reconcile.

These tests pin:
  * the dispatched method/path/params (signed GET /fapi/v1/order with
    ``origClientOrderId``),
  * parsing of a FILLED body into ``FuturesDemoOrderStatusResult``,
  * NEW status surfaced verbatim (so the caller can keep polling),
  * caller-bug validation (empty symbol / client_order_id) before any HTTP.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderStatusResult,
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
async def test_get_order_dispatches_signed_get_with_orig_client_order_id(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """``get_order`` dispatches a signed GET /fapi/v1/order keyed by client id."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        status_code=200,
        json={
            "symbol": "XRPUSDT",
            "orderId": 99,
            "clientOrderId": "rob305-open-cid",
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "30",
            "executedQty": "30",
            "avgPrice": "0.5000",
            "reduceOnly": False,
        },
    )
    await client.get_order(symbol="XRPUSDT", client_order_id="rob305-open-cid")

    last = httpx_mock.get_request()
    assert last is not None
    assert last.method == "GET"
    assert last.url.path == "/fapi/v1/order"
    url_str = str(last.url)
    assert "origClientOrderId=rob305-open-cid" in url_str
    assert "signature=" in url_str
    assert "timestamp=" in url_str


@pytest.mark.asyncio
async def test_get_order_parses_filled_body(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """A FILLED order body parses into a FuturesDemoOrderStatusResult."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        status_code=200,
        json={
            "symbol": "XRPUSDT",
            "orderId": 1234,
            "clientOrderId": "rob305-open-cid",
            "status": "FILLED",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "30",
            "executedQty": "30",
            "avgPrice": "0.5012",
            "reduceOnly": False,
        },
    )
    result = await client.get_order(symbol="XRPUSDT", client_order_id="rob305-open-cid")
    assert isinstance(result, FuturesDemoOrderStatusResult)
    assert result.client_order_id == "rob305-open-cid"
    assert result.broker_order_id == "1234"
    assert result.symbol == "XRPUSDT"
    assert result.status == "FILLED"
    assert result.executed_qty == Decimal("30")
    assert result.avg_price == Decimal("0.5012")
    assert result.reduce_only is False


@pytest.mark.asyncio
async def test_get_order_surfaces_new_status_verbatim(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """A still-NEW order surfaces ``status=NEW`` so the caller keeps polling."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        status_code=200,
        json={
            "symbol": "XRPUSDT",
            "orderId": 1234,
            "clientOrderId": "rob305-open-cid",
            "status": "NEW",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "30",
            "executedQty": "0",
            "avgPrice": "0",
            "reduceOnly": False,
        },
    )
    result = await client.get_order(symbol="XRPUSDT", client_order_id="rob305-open-cid")
    assert result.status == "NEW"
    assert result.executed_qty == Decimal("0")


@pytest.mark.asyncio
async def test_general_get_order_keeps_legacy_defaults_for_sparse_body(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Strict reconciliation reads raw_response; the polling DTO stays compatible."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        status_code=200,
        json={"status": "CANCELED"},
    )

    result = await client.get_order(
        symbol="XRPUSDT", client_order_id="rob305-sparse-cid"
    )

    assert result.client_order_id == "rob305-sparse-cid"
    assert result.symbol == "XRPUSDT"
    assert result.executed_qty == Decimal("0")
    assert result.raw_response_redacted == {"status": "CANCELED"}


@pytest.mark.asyncio
async def test_get_order_rejects_empty_args_before_http(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    """Empty symbol / client_order_id is a caller bug — fail before signing."""
    with pytest.raises(ValueError):
        await client.get_order(symbol="", client_order_id="cid")
    with pytest.raises(ValueError):
        await client.get_order(symbol="XRPUSDT", client_order_id="")
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_get_order_normalizes_explicit_binance_not_found(
    client: BinanceFuturesDemoExecutionClient, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/order\?.*$"),
        status_code=400,
        json={"code": -2013, "msg": "Order does not exist."},
    )

    with pytest.raises(BinanceDemoOrderNotFound):
        await client.get_order(symbol="XRPUSDT", client_order_id="missing-cid")
