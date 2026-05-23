"""ROB-299 — get_asset_balance narrow signed read-side method."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.dto import SpotDemoAssetBalance
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)

_BASE = "https://demo-api.binance.com"

_ACCOUNT_JSON = {
    "canTrade": True,
    "accountType": "SPOT",
    "balances": [
        {"asset": "XRP", "free": "12.34000000", "locked": "0.00000000"},
        {"asset": "USDT", "free": "500.00000000", "locked": "0.00000000"},
    ],
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> BinanceSpotDemoExecutionClient:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "DUMMY_SECRET")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", _BASE)
    return BinanceSpotDemoExecutionClient.from_env()


@pytest.mark.asyncio
async def test_get_asset_balance_returns_only_requested_asset(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/account\?.*$"),
        json=_ACCOUNT_JSON,
    )
    bal = await client.get_asset_balance(asset="XRP")
    assert isinstance(bal, SpotDemoAssetBalance)
    assert bal.asset == "XRP"
    assert bal.free == Decimal("12.34000000")
    assert bal.locked == Decimal("0")


@pytest.mark.asyncio
async def test_get_asset_balance_absent_asset_returns_zero(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/account\?.*$"),
        json=_ACCOUNT_JSON,
    )
    bal = await client.get_asset_balance(asset="DOGE")
    assert bal.asset == "DOGE"
    assert bal.free == Decimal("0")
    assert bal.locked == Decimal("0")
