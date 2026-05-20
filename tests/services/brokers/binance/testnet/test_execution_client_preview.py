"""ROB-286 — Execution client preview / dry-run path.

Matrix row T13.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.testnet.dto import DryRunResult, OrderPreview
from app.services.brokers.binance.testnet.execution_client import (
    BinanceTestnetExecutionClient,
)


@pytest.fixture
def client(monkeypatch) -> BinanceTestnetExecutionClient:
    """Construct an execution client with dummy credentials."""
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    return BinanceTestnetExecutionClient.from_env()


@pytest.mark.asyncio
async def test_dry_run_no_http(client: BinanceTestnetExecutionClient, httpx_mock):
    """T13 — submit_order(confirm=False) does not hit HTTP.

    Uses ``httpx_mock`` with no responses registered. If any HTTP call
    leaks, the test would error out with "No response registered".
    """
    result = await client.submit_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        notional_usdt=Decimal("5"),
    )
    assert isinstance(result, DryRunResult)
    assert "confirm=False" in result.reason
    assert result.preview.symbol == "BTCUSDT"
    assert result.preview.side == "BUY"
    # No HTTP requests were issued.
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_preview_order_pure_validation(client: BinanceTestnetExecutionClient):
    """preview_order returns an OrderPreview; no HTTP at all."""
    preview = await client.preview_order(
        symbol="ETHUSDT",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.05"),
        notional_usdt=Decimal("7"),
    )
    assert isinstance(preview, OrderPreview)
    assert preview.symbol == "ETHUSDT"
    assert preview.side == "SELL"
    assert preview.order_type == "MARKET"
    assert preview.price is None
    # Auto-generated client order id is a 32-char UUID hex.
    assert len(preview.client_order_id) == 32


@pytest.mark.asyncio
async def test_cancel_order_dry_run_no_http(
    client: BinanceTestnetExecutionClient, httpx_mock
):
    """cancel_order(confirm=False) does not hit HTTP."""
    result = await client.cancel_order(symbol="BTCUSDT", client_order_id="abc123")
    assert isinstance(result, DryRunResult)
    assert "confirm=False" in result.reason
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_limit_order_requires_price(client: BinanceTestnetExecutionClient):
    """A LIMIT order without price is rejected at preview time."""
    with pytest.raises(ValueError, match="LIMIT order requires explicit price"):
        await client.preview_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.001"),
            notional_usdt=Decimal("5"),
        )


@pytest.mark.asyncio
async def test_market_order_rejects_price(client: BinanceTestnetExecutionClient):
    """A MARKET order with price is rejected at preview time."""
    with pytest.raises(ValueError, match="MARKET order must not carry a price"):
        await client.preview_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("0.001"),
            price=Decimal("50000"),
            notional_usdt=Decimal("5"),
        )


@pytest.mark.asyncio
async def test_invalid_side_rejected(client: BinanceTestnetExecutionClient):
    with pytest.raises(ValueError):
        await client.preview_order(
            symbol="BTCUSDT",
            side="HOLD",  # not in BUY/SELL
            order_type="MARKET",
            quantity=Decimal("0.001"),
            notional_usdt=Decimal("5"),
        )


@pytest.mark.asyncio
async def test_repr_does_not_contain_secret(client: BinanceTestnetExecutionClient):
    """Defense in depth — repr() must not contain the API secret."""
    rendered = repr(client)
    assert "DUMMY_SECRET" not in rendered
