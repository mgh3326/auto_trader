from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
)
from app.services.manual_holdings_service import ManualHoldingsService


@pytest.mark.asyncio
async def test_create_holding(mock_db):
    # Setup
    account = BrokerAccount(
        id=1,
        user_id=1,
        broker_type=BrokerType.toss,
        account_name="Test Account",
    )

    service = ManualHoldingsService(mock_db)

    # Test
    holding = await service.create_holding(
        broker_account_id=account.id,
        ticker="AAPL",
        market_type=MarketType.US,
        quantity=10,
        avg_price=150.0,
        display_name="Apple",
    )

    # Verify
    assert holding.ticker == "AAPL"
    assert holding.quantity == 10
    assert holding.avg_price == 150.0

    # Verify DB interactions
    mock_db.add.assert_called_once()
    mock_db.commit.assert_called_once()
    mock_db.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_bulk_create_holdings_atomicity(mock_db):
    # Setup
    account_id = 1
    service = ManualHoldingsService(mock_db)

    holdings_data = [
        {
            "ticker": "TSLA",
            "market_type": MarketType.US,
            "quantity": 5,
            "avg_price": 200.0,
        },
        {
            "ticker": "NVDA",
            "market_type": MarketType.US,
            "quantity": 2,
            "avg_price": 400.0,
        },
    ]

    # Mock get_holding_by_ticker to return None (new holdings)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    # Configure begin_nested to return an async context manager
    # mock_db.begin_nested is likely an AsyncMock, so calling it returns a coroutine.
    # We need it to return an object that can be used in 'async with'.
    # Since AsyncSession.begin_nested() is not async (it returns the context manager directly),
    # we should make mock_db.begin_nested a MagicMock.

    mock_db.begin_nested = MagicMock()
    nested_tx = AsyncMock()
    mock_db.begin_nested.return_value = nested_tx

    # Test Success
    created = await service.bulk_create_holdings(account_id, holdings_data)
    assert len(created) == 2

    # Verify transaction usage
    assert mock_db.begin_nested.called
    assert mock_db.flush.called
    assert mock_db.refresh.call_count == 2


@pytest.mark.asyncio
async def test_get_holdings_by_user(mock_db):
    # Setup
    service = ManualHoldingsService(mock_db)

    # Mock DB response
    holding = ManualHolding(
        id=1,
        broker_account_id=1,
        ticker="GOOGL",
        market_type=MarketType.US,
        quantity=5,
        avg_price=100.0,
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [holding]
    mock_db.execute.return_value = mock_result

    # Test
    holdings = await service.get_holdings_by_user(999)
    assert len(holdings) == 1
    assert holdings[0].ticker == "GOOGL"
