from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.invest_view_model.stock_detail_candles_service import (
    UnsupportedPeriod,
    build_stock_detail_candles,
)


@pytest.mark.asyncio
async def test_crypto_intraday_period_is_explicitly_unsupported():
    with pytest.raises(UnsupportedPeriod):
        await build_stock_detail_candles(market="crypto", symbol="BTC-KRW", period="5m")


@pytest.mark.asyncio
async def test_candles_response_maps_provider_rows():
    async def provider(market, symbol, period):
        return [
            {
                "ts": datetime.now(UTC),
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "volume": 10,
            }
        ]

    response = await build_stock_detail_candles(
        market="us",
        symbol="BRK-B",
        period="1d",
        provider=provider,
    )

    assert response.symbol == "BRK.B"
    assert response.market == "us"
    assert response.period == "1d"
    assert response.candles[0].close == 1.5
    assert response.capabilities.intradaySupported is True
