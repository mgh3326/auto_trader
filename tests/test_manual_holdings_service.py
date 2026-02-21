from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
)
from app.services import manual_holdings_service as manual_holdings_module
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.us_symbol_universe_service import USSymbolNotRegisteredError


@pytest.mark.asyncio
async def test_create_holding(mock_db, monkeypatch):
    # Setup
    account = BrokerAccount(
        id=1,
        user_id=1,
        broker_type=BrokerType.TOSS,
        account_name="Test Account",
    )

    monkeypatch.setattr(
        manual_holdings_module,
        "get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
        raising=False,
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
async def test_bulk_create_holdings_atomicity(mock_db, monkeypatch):
    # Setup
    account_id = 1
    service = ManualHoldingsService(mock_db)

    monkeypatch.setattr(
        manual_holdings_module,
        "get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
        raising=False,
    )

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


@pytest.mark.asyncio
async def test_upsert_holding_raises_validation_error_for_invalid_us_symbol(
    mock_db, monkeypatch
):
    service = ManualHoldingsService(mock_db)

    monkeypatch.setattr(
        manual_holdings_module,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=USSymbolNotRegisteredError("US symbol not registered")),
        raising=False,
    )

    with pytest.raises(manual_holdings_module.ManualHoldingValidationError):
        await service.upsert_holding(
            broker_account_id=1,
            ticker="솔라나",
            market_type=MarketType.US,
            quantity=1,
            avg_price=10,
        )


@pytest.mark.asyncio
async def test_upsert_holding_normalizes_us_ticker_to_db_symbol(mock_db, monkeypatch):
    service = ManualHoldingsService(mock_db)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    monkeypatch.setattr(
        manual_holdings_module,
        "get_us_exchange_by_symbol",
        AsyncMock(return_value="NYSE"),
        raising=False,
    )

    holding = await service.upsert_holding(
        broker_account_id=1,
        ticker="BRK-B",
        market_type=MarketType.US,
        quantity=1,
        avg_price=400,
    )

    assert holding.ticker == "BRK.B"


@pytest.mark.asyncio
async def test_upsert_holding_raises_validation_error_for_inactive_crypto_symbol(
    mock_db, monkeypatch
):
    service = ManualHoldingsService(mock_db)

    monkeypatch.setattr(
        manual_holdings_module,
        "get_active_upbit_markets",
        AsyncMock(return_value=["KRW-BTC"]),
        raising=False,
    )

    with pytest.raises(manual_holdings_module.ManualHoldingValidationError):
        await service.upsert_holding(
            broker_account_id=1,
            ticker="KRW-SOL",
            market_type=MarketType.CRYPTO,
            quantity=1,
            avg_price=100,
        )


@pytest.mark.asyncio
async def test_bulk_create_holdings_fails_whole_request_on_mixed_valid_invalid(
    mock_db, monkeypatch
):
    service = ManualHoldingsService(mock_db)
    mock_db.begin_nested = MagicMock()

    async def _mock_get_us_exchange(symbol: str, db=None):
        if symbol == "AAPL":
            return "NASD"
        raise USSymbolNotRegisteredError("US symbol not registered")

    monkeypatch.setattr(
        manual_holdings_module,
        "get_us_exchange_by_symbol",
        _mock_get_us_exchange,
        raising=False,
    )

    holdings_data = [
        {
            "ticker": "AAPL",
            "market_type": MarketType.US,
            "quantity": 1,
            "avg_price": 100,
        },
        {
            "ticker": "솔라나",
            "market_type": MarketType.US,
            "quantity": 1,
            "avg_price": 100,
        },
    ]

    with pytest.raises(manual_holdings_module.ManualHoldingValidationError):
        await service.bulk_create_holdings(1, holdings_data)

    mock_db.add.assert_not_called()
