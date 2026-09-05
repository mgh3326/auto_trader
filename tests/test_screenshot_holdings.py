"""Service-only holding calculation regressions retained after MCP cleanup."""

import pytest

from app.services.screenshot_holdings_service import ScreenshotHoldingsService


@pytest.mark.asyncio
async def test_update_manual_holdings_calculate_avg_buy_price():
    """Test average buy price calculation from eval_amount and profit_loss."""
    service = ScreenshotHoldingsService(object())

    avg_price = await service._calculate_avg_buy_price(
        eval_amount=1500000, profit_loss=100000, quantity=10
    )

    assert avg_price == pytest.approx(140000.0)


@pytest.mark.asyncio
async def test_update_manual_holdings_calculate_avg_buy_price_zero_quantity():
    """Test average buy price with zero quantity."""
    service = ScreenshotHoldingsService(object())

    avg_price = await service._calculate_avg_buy_price(
        eval_amount=0, profit_loss=0, quantity=0
    )

    assert avg_price == pytest.approx(0.0)
