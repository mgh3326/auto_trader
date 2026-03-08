"""Exception handling tests for KIS trading service (Stage 1 hardening).

These tests verify that exceptions are properly caught and returned as
structured error payloads instead of propagating to callers.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.analysis import StockAnalysisResult
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis,
)


@pytest.fixture
def mock_kis_client():
    client = AsyncMock()
    client.order_korea_stock.return_value = {
        "odno": "0001234567",
        "ord_tmd": "091500",
        "msg": "Success",
    }
    client.order_overseas_stock.return_value = {
        "odno": "0001234567",
        "ord_tmd": "091500",
        "msg": "Success",
    }
    client.get_balance.return_value = {"output2": [{"dnca_tot_amt": "1000000"}]}
    return client


class TestDomesticBuyExceptionHandling:
    """Exception handling tests for domestic buy orders."""

    @pytest.mark.asyncio
    async def test_api_error_returns_error_payload(self, mock_kis_client):
        """API error during domestic buy should return error payload."""
        mock_kis_client.order_korea_stock = AsyncMock(
            side_effect=RuntimeError("APBK0400 API error: insufficient balance")
        )

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.stock_info_service.StockAnalysisService"
            ) as mock_service_cls,
            patch(
                "app.services.symbol_trade_settings_service.get_buy_quantity_for_symbol"
            ) as mock_get_qty,
            patch(
                "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
            ) as mock_settings_service_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_service_cls.return_value = mock_settings_service
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 4
            mock_settings.buy_quantity_per_order = 2
            mock_settings_service.get_by_symbol.return_value = mock_settings

            mock_get_qty.return_value = 2

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=50000,
                appropriate_buy_max=52000,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_domestic_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005930",
                current_price=51000,
                avg_buy_price=60000,
            )

            # Exception should be caught, not propagated
            assert result["success"] is False
            assert result["error_type"] == "api"
            assert "APBK0400" in result["error"]
            assert result["orders_placed"] == 0


class TestDomesticSellExceptionHandling:
    """Exception handling tests for domestic sell orders."""

    @pytest.mark.asyncio
    async def test_api_error_returns_error_payload(self, mock_kis_client):
        """API error during domestic sell should return error payload."""
        mock_kis_client.order_korea_stock = AsyncMock(
            side_effect=RuntimeError("APBK0400 insufficient orderable quantity")
        )

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.stock_info_service.StockAnalysisService"
            ) as mock_service_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            # Provide analysis so order execution path is taken
            analysis = StockAnalysisResult(
                decision="sell",
                appropriate_sell_min=60000,
                appropriate_sell_max=62000,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_domestic_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005930",
                current_price=55000,
                avg_buy_price=50000,
                balance_qty=10,
            )

            # Exception should be caught, not propagated
            assert result["success"] is False
            assert result["error_type"] == "api"
            assert "APBK0400" in result["error"]
            assert result["orders_placed"] == 0


class TestOverseasBuyExceptionHandling:
    """Exception handling tests for overseas buy orders."""

    @pytest.mark.asyncio
    async def test_api_error_returns_error_payload(self, mock_kis_client):
        """API error during overseas buy should return error payload."""
        mock_kis_client.order_overseas_stock = AsyncMock(
            side_effect=RuntimeError("APBK0400 API error")
        )

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.stock_info_service.StockAnalysisService"
            ) as mock_service_cls,
            patch(
                "app.services.symbol_trade_settings_service.get_buy_quantity_for_symbol"
            ) as mock_get_qty,
            patch(
                "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
            ) as mock_settings_service_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_service_cls.return_value = mock_settings_service
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 4
            mock_settings.buy_quantity_per_order = 2
            mock_settings_service.get_by_symbol.return_value = mock_settings

            mock_get_qty.return_value = 2

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=50,
                appropriate_buy_max=55,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="AAPL",
                current_price=60,
                avg_buy_price=80,
                exchange_code="NASD",
            )

            # Exception should be caught, not propagated
            assert result["success"] is False
            assert result["error_type"] == "api"
            assert "APBK0400" in result["error"]
            assert result["orders_placed"] == 0


class TestOverseasSellExceptionHandling:
    """Exception handling tests for overseas sell orders."""

    @pytest.mark.asyncio
    async def test_api_error_returns_error_payload(self, mock_kis_client):
        """API error during overseas sell should return error payload."""
        mock_kis_client.order_overseas_stock = AsyncMock(
            side_effect=RuntimeError("APBK0400 insufficient orderable quantity")
        )
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_cblc_qty": "10",
                    "ord_psbl_qty": "10",
                    "pchs_avg_pric": "150.0",
                    "now_pric2": "200.0",
                    "ovrs_excg_cd": "NASD",
                }
            ]
        )

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.stock_info_service.StockAnalysisService"
            ) as mock_service_cls,
            patch(
                "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
            ) as mock_settings_service_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_service_cls.return_value = mock_settings_service
            mock_settings_service.get_by_symbol.return_value = None

            analysis = StockAnalysisResult(
                decision="sell",
                appropriate_sell_min=300.0,
                appropriate_sell_max=320.0,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="AAPL",
                current_price=200.0,
                avg_buy_price=150.0,
                balance_qty=10,
                exchange_code="NASD",
            )

            # Exception should be caught, not propagated
            assert result["success"] is False
            assert result["error_type"] == "api"
            assert "APBK0400" in result["error"]
            assert result["orders_placed"] == 0
