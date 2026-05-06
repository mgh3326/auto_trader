# tests/test_kis_order_ops.py
from unittest.mock import AsyncMock

import pytest

from app.services.kis_trading_service import (
    _DOMESTIC_OPS,
    _OVERSEAS_OPS,
)


class TestDomesticOrderOps:
    def test_singleton_exists(self):
        assert _DOMESTIC_OPS.market == "domestic"

    @pytest.mark.asyncio
    async def test_place_order_calls_korea_stock(self):
        kis = AsyncMock()
        kis.order_korea_stock.return_value = {"odno": "123"}

        result = await _DOMESTIC_OPS.place_order(
            kis, "005930", "buy", 10, 50000.0, exchange_code=None
        )

        kis.order_korea_stock.assert_called_once_with(
            stock_code="005930", order_type="buy", quantity=10, price=50000
        )
        assert result == {"odno": "123"}

    @pytest.mark.asyncio
    async def test_place_order_casts_price_to_int(self):
        kis = AsyncMock()
        kis.order_korea_stock.return_value = {"odno": "123"}

        await _DOMESTIC_OPS.place_order(kis, "005930", "buy", 1, 73800.5)
        call_args = kis.order_korea_stock.call_args
        assert call_args.kwargs["price"] == 73800

    @pytest.mark.asyncio
    async def test_adjust_sell_qty_returns_unchanged(self):
        kis = AsyncMock()
        result = await _DOMESTIC_OPS.adjust_sell_qty(kis, "005930", 10)
        assert result == 10

    def test_resolve_exchange_code_returns_none(self):
        assert _DOMESTIC_OPS.resolve_exchange_code(None, "NASD") is None


class TestOverseasOrderOps:
    def test_singleton_exists(self):
        assert _OVERSEAS_OPS.market == "overseas"

    @pytest.mark.asyncio
    async def test_place_order_calls_overseas_stock(self):
        kis = AsyncMock()
        kis.order_overseas_stock.return_value = {"odno": "456"}

        result = await _OVERSEAS_OPS.place_order(
            kis, "AAPL", "buy", 5, 175.50, exchange_code="NASD"
        )

        kis.order_overseas_stock.assert_called_once_with(
            symbol="AAPL",
            exchange_code="NASD",
            order_type="buy",
            quantity=5,
            price=175.50,
        )
        assert result == {"odno": "456"}

    @pytest.mark.asyncio
    async def test_place_order_keeps_float_price(self):
        kis = AsyncMock()
        kis.order_overseas_stock.return_value = {"odno": "456"}

        await _OVERSEAS_OPS.place_order(
            kis, "AAPL", "buy", 1, 175.99, exchange_code="NASD"
        )
        call_args = kis.order_overseas_stock.call_args
        assert call_args.kwargs["price"] == pytest.approx(175.99)

    @pytest.mark.asyncio
    async def test_adjust_sell_qty_reduces_when_account_has_less(self):
        kis = AsyncMock()
        kis.fetch_my_overseas_stocks.return_value = [
            {"ovrs_pdno": "AAPL", "ord_psbl_qty": "7", "ovrs_cblc_qty": "10"}
        ]

        result = await _OVERSEAS_OPS.adjust_sell_qty(kis, "AAPL", 10)
        assert result == 7

    @pytest.mark.asyncio
    async def test_adjust_sell_qty_unchanged_when_account_has_more(self):
        kis = AsyncMock()
        kis.fetch_my_overseas_stocks.return_value = [
            {"ovrs_pdno": "AAPL", "ord_psbl_qty": "15", "ovrs_cblc_qty": "15"}
        ]

        result = await _OVERSEAS_OPS.adjust_sell_qty(kis, "AAPL", 10)
        assert result == 10

    def test_resolve_exchange_code_from_settings(self):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.exchange_code = "NYSE"
        assert _OVERSEAS_OPS.resolve_exchange_code(settings, "NASD") == "NYSE"

    def test_resolve_exchange_code_fallback(self):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.exchange_code = None
        assert _OVERSEAS_OPS.resolve_exchange_code(settings, "NASD") == "NASD"

    def test_resolve_exchange_code_no_settings(self):
        assert _OVERSEAS_OPS.resolve_exchange_code(None, "AMEX") == "AMEX"
