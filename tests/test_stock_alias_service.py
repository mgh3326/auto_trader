from unittest.mock import MagicMock

import pytest

from app.models.manual_holdings import MarketType
from app.services.stock_alias_service import StockAliasService


@pytest.mark.asyncio
async def test_get_ticker_by_alias_falls_back_to_default_aliases(mock_db):
    service = StockAliasService(mock_db)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    ticker = await service.get_ticker_by_alias("DIREXION TESLA 2X", MarketType.US)

    assert ticker == "TSLL"


@pytest.mark.asyncio
async def test_get_ticker_by_alias_prefers_db_alias_over_default(mock_db):
    service = StockAliasService(mock_db)

    db_alias = MagicMock()
    db_alias.ticker = "TSL2"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = db_alias
    mock_db.execute.return_value = mock_result

    ticker = await service.get_ticker_by_alias("DIREXION TESLA 2X", MarketType.US)

    assert ticker == "TSL2"
