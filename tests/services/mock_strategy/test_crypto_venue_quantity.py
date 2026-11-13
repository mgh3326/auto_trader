"""A DB-read quantity reaches the Demo submit as venue text, not storage padding.

``review.mock_strategy_orders.qty`` is ``Numeric(38, 18)``, so a planned
``0.00398`` comes back as ``Decimal("0.003980000000000000")``. Signed as
``format(qty, "f")`` that 18-decimal string is rejected by Binance on precision.
The fix is applied where the DB-read quantity enters ``BinanceCryptoDemo.submit``,
so the Demo clients' own wire format stays unchanged for every other caller.
"""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.services.brokers.binance.demo.quantity import venue_order_quantity
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.mock_strategy.crypto_brokers import (
    BinanceCryptoDemo,
    PreDispatchRefusal,
)

pytestmark = pytest.mark.unit

_SPOT_DEMO_BASE = "https://demo-api.binance.com"


@pytest.mark.parametrize(
    "stored,expected_text",
    [
        (Decimal("0.003980000000000000"), "0.00398"),
        (Decimal("12.000000000000000000"), "12"),
        (Decimal("100.000"), "100"),
        (Decimal("0.500000000000000000"), "0.5"),
        (Decimal("0.0001"), "0.0001"),
        (Decimal("1000"), "1000"),
    ],
)
def test_venue_order_quantity_is_plain_text_with_the_same_value(
    stored: Decimal, expected_text: str
) -> None:
    rendered = venue_order_quantity(stored)
    assert format(rendered, "f") == expected_text
    assert rendered == stored


class _FakeClient:
    credential_fingerprint = "fixture-fingerprint"

    def __init__(self, *, futures: bool):
        self.futures = futures
        self.calls: list[dict] = []

    async def get_symbol_info(self, **kwargs):
        return {
            "status": "TRADING",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.00001",
                    "minQty": "0.00001",
                    "maxQty": "1000",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }

    async def get_ticker_price(self, **kwargs):
        # 0.00398 at 50000 is 199 USDT, above the 5 USDT minimum notional.
        return Decimal("50000")

    async def get_position_mode(self):
        return SimpleNamespace(is_hedge_mode=False)

    async def get_position(self, *, symbol):
        return SimpleNamespace(
            symbol=symbol,
            require_position_side=lambda: "BOTH",
            position_amt=Decimal("0"),
            leverage=1,
            margin_type="isolated",
        )

    async def submit_order(self, **kwargs):
        self.calls.append(kwargs)
        qty_text = format(kwargs["qty"], "f")
        return SimpleNamespace(
            raw_response_redacted={
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "clientOrderId": kwargs["client_order_id"],
                "orderId": 42,
                "origQty": qty_text,
                "executedQty": qty_text,
                "avgPrice": "1000",
                "cummulativeQuoteQty": "2000",
                "status": "FILLED",
                "positionSide": "BOTH",
                "reduceOnly": False,
            }
        )


@pytest.mark.parametrize("futures", [False, True])
@pytest.mark.asyncio
async def test_storage_scale_quantity_is_submitted_as_venue_text(monkeypatch, futures):
    monkeypatch.setenv("MOCK_STRATEGY_ENABLED", "true")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_ENABLED" if futures else "BINANCE_SPOT_DEMO_ENABLED",
        "true",
    )
    client = _FakeClient(futures=futures)
    broker = BinanceCryptoDemo(
        client, futures=futures, expected_fingerprint="fixture-fingerprint"
    )
    order = SimpleNamespace(
        symbol="BTCUSDT",
        side="short",
        qty=Decimal("0.003980000000000000"),
        client_order_id="r1474c-e-storage-scale",
    )
    await broker.submit(order, confirm=True)
    assert len(client.calls) == 1
    sent = client.calls[0]["qty"]
    assert format(sent, "f") == "0.00398"
    assert sent == order.qty
    assert order.qty.as_tuple().exponent == -18


@pytest.mark.asyncio
async def test_spot_demo_wire_body_carries_venue_text(monkeypatch, httpx_mock):
    monkeypatch.setenv("MOCK_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_SPOT_DEMO_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SPOT_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _SPOT_DEMO_BASE)
    client = BinanceSpotDemoExecutionClient.from_env()
    broker = BinanceCryptoDemo(
        client, futures=False, expected_fingerprint=client.credential_fingerprint
    )
    cid = "r1474c-e-wire-spot"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/exchangeInfo.*$"),
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "filters": [
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": "0.00001",
                            "minQty": "0.00001",
                            "maxQty": "1000",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/ticker/price.*$"),
        json={"symbol": "BTCUSDT", "price": "50000"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/myTrades.*$"),
        json=[
            {
                "symbol": "BTCUSDT",
                "orderId": 12345,
                "isBuyer": True,
                "qty": "0.00398",
                "commission": "0",
                "commissionAsset": "USDT",
            }
        ],
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$"),
        json={
            "symbol": "BTCUSDT",
            "orderId": 12345,
            "clientOrderId": cid,
            "origQty": "0.00398",
            "executedQty": "0.00398",
            "cummulativeQuoteQty": "199.00",
            "status": "FILLED",
            "type": "MARKET",
            "side": "BUY",
            "fills": [
                {
                    "price": "50000",
                    "qty": "0.00398",
                    "commission": "0",
                    "commissionAsset": "USDT",
                }
            ],
        },
        status_code=200,
    )
    order = SimpleNamespace(
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("0.003980000000000000"),
        client_order_id=cid,
    )
    proof = await broker.submit(order, confirm=True)
    assert proof.filled_qty == order.qty
    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 1
    assert parse_qs(urlparse(str(posts[0].url)).query)["quantity"] == ["0.00398"]


@pytest.mark.parametrize("futures,side", [(False, "buy"), (True, "short")])
@pytest.mark.asyncio
async def test_off_grid_storage_quantity_still_refused_before_submit(
    monkeypatch, futures, side
):
    monkeypatch.setenv("MOCK_STRATEGY_ENABLED", "true")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_ENABLED" if futures else "BINANCE_SPOT_DEMO_ENABLED",
        "true",
    )
    client = _FakeClient(futures=futures)
    broker = BinanceCryptoDemo(
        client, futures=futures, expected_fingerprint="fixture-fingerprint"
    )
    off_grid = SimpleNamespace(
        symbol="BTCUSDT",
        side=side,
        qty=Decimal("0.003985000000000000"),
        client_order_id="r1474c-e-off-grid-storage",
    )
    with pytest.raises(PreDispatchRefusal):
        await broker.submit(off_grid, confirm=True)
    assert client.calls == []
