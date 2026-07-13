"""Spot Demo single-order broker truth used by reservation reconciliation."""

from __future__ import annotations

import re

import pytest

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceSpotDemoExecutionClient:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_SPOT_DEMO_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SPOT_DEMO_SECRET")
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_get_order_status_normalizes_explicit_binance_not_found(
    client: BinanceSpotDemoExecutionClient, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/order\?.*$"),
        status_code=400,
        json={"code": -2013, "msg": "Order does not exist."},
    )

    with pytest.raises(BinanceDemoOrderNotFound):
        await client.get_order_status(symbol="XRPUSDT", client_order_id="missing-cid")
