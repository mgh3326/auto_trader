"""ROB-307 PR3 — spot OCO bracket (TP limit + SL stop-limit).

Spot has no reduceOnly, so the broker-side bracket on a held long is a
SELL OCO: a LIMIT take-profit (above) one-cancels-other a STOP_LOSS_LIMIT
(below). Signed POST /api/v3/order/oco; dry-run with zero HTTP by default.
Demo host only; network mocked.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.dto import SpotDemoOcoResult
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
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
async def test_sell_oco_dispatches_signed_two_legs(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order/oco\?.*$"),
        json={
            "orderListId": 555,
            "listClientOrderId": "list-1",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {"symbol": "XRPUSDT", "orderId": 1, "clientOrderId": "tp-1"},
                {"symbol": "XRPUSDT", "orderId": 2, "clientOrderId": "sl-1"},
            ],
        },
    )
    result = await client.submit_oco(
        symbol="XRPUSDT",
        side="SELL",
        quantity=Decimal("7.3"),
        tp_price=Decimal("1.40"),
        sl_stop_price=Decimal("1.33"),
        sl_limit_price=Decimal("1.32"),
        confirm=True,
    )
    assert isinstance(result, SpotDemoOcoResult)
    assert result.order_list_id == "555"
    assert set(result.leg_client_order_ids) == {"tp-1", "sl-1"}
    url = str(httpx_mock.get_requests()[0].url)
    assert "price=1.40" in url  # TP limit leg
    assert "stopPrice=1.33" in url  # SL trigger
    assert "stopLimitPrice=1.32" in url  # SL limit
    assert "stopLimitTimeInForce=GTC" in url


@pytest.mark.asyncio
async def test_oco_dry_run_dispatches_zero_http(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    result = await client.submit_oco(
        symbol="XRPUSDT",
        side="SELL",
        quantity=Decimal("7.3"),
        tp_price=Decimal("1.40"),
        sl_stop_price=Decimal("1.33"),
        sl_limit_price=Decimal("1.32"),
    )
    assert isinstance(result, SpotDemoDryRunResult)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_oco_rejects_tp_not_above_sl_for_sell(
    client: BinanceSpotDemoExecutionClient,
) -> None:
    # SELL bracket: TP must be above the SL trigger.
    with pytest.raises(ValueError):
        await client.submit_oco(
            symbol="XRPUSDT",
            side="SELL",
            quantity=Decimal("7.3"),
            tp_price=Decimal("1.30"),
            sl_stop_price=Decimal("1.33"),
            sl_limit_price=Decimal("1.32"),
            confirm=True,
        )


@pytest.mark.asyncio
async def test_oco_rejects_nonpositive_quantity(
    client: BinanceSpotDemoExecutionClient,
) -> None:
    with pytest.raises(ValueError):
        await client.submit_oco(
            symbol="XRPUSDT",
            side="SELL",
            quantity=Decimal("0"),
            tp_price=Decimal("1.40"),
            sl_stop_price=Decimal("1.33"),
            sl_limit_price=Decimal("1.32"),
            confirm=True,
        )
