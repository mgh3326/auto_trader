"""Tests for get_orderbook market type mismatch handling.

Fixes: AUTO_TRADER-4G, AUTO_TRADER-4H
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests._mcp_tooling_support import build_tools


class TestGetOrderbookMarketMismatch:
    """Test that crypto symbols with market='kr' are handled correctly."""

    @pytest.mark.asyncio
    async def test_get_orderbook_crypto_symbol_with_kr_market_auto_routes_to_crypto(
        self,
    ):
        """KRW-DOT with market='kr' should auto-route to crypto, not fail validation."""
        tools = build_tools()
        get_orderbook = tools["get_orderbook"]

        # Mock the market data service to avoid external API calls
        mock_snapshot = {
            "symbol": "KRW-DOT",
            "asks": [{"price": 15000.0, "quantity": 1.5}],
            "bids": [{"price": 14900.0, "quantity": 2.0}],
            "total_ask_qty": 10.0,
            "total_bid_qty": 15.0,
        }

        with patch(
            "app.mcp_server.tooling.market_data_quotes.market_data_service.get_orderbook",
            new_callable=AsyncMock,
            return_value=mock_snapshot,
        ):
            # This should NOT raise ValueError about Korean equity symbols
            result = await get_orderbook(symbol="KRW-DOT", market="kr")

            # Should succeed and return crypto-formatted result
            assert result["symbol"] == "KRW-DOT"
            assert result["instrument_type"] == "crypto"
            assert result["source"] == "upbit"

    @pytest.mark.asyncio
    async def test_get_orderbook_usdt_symbol_with_kr_market_auto_routes_to_crypto(self):
        """USDT-BTC with market='kr' should also auto-route to crypto."""
        tools = build_tools()
        get_orderbook = tools["get_orderbook"]

        mock_snapshot = {
            "symbol": "USDT-BTC",
            "asks": [{"price": 65000.0, "quantity": 0.5}],
            "bids": [{"price": 64900.0, "quantity": 0.3}],
            "total_ask_qty": 5.0,
            "total_bid_qty": 3.0,
        }

        with patch(
            "app.mcp_server.tooling.market_data_quotes.market_data_service.get_orderbook",
            new_callable=AsyncMock,
            return_value=mock_snapshot,
        ):
            result = await get_orderbook(symbol="USDT-BTC", market="kr")

            assert result["symbol"] == "USDT-BTC"
            assert result["instrument_type"] == "crypto"
            assert result["source"] == "upbit"

    @pytest.mark.asyncio
    async def test_get_orderbook_kr_equity_with_kr_market_still_works(self):
        """Regular KR equity codes with market='kr' should still work."""
        tools = build_tools()
        get_orderbook = tools["get_orderbook"]

        mock_snapshot = {
            "symbol": "005930",
            "asks": [{"price": 70100, "quantity": 123}],
            "bids": [{"price": 70000, "quantity": 321}],
            "total_ask_qty": 1000,
            "total_bid_qty": 1500,
        }

        with patch(
            "app.mcp_server.tooling.market_data_quotes.market_data_service.get_orderbook",
            new_callable=AsyncMock,
            return_value=mock_snapshot,
        ):
            result = await get_orderbook(symbol="005930", market="kr")

            assert result["symbol"] == "005930"
            assert result["instrument_type"] == "equity_kr"
            assert result["source"] == "kis"
