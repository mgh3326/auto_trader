"""Tests for KRX tick size adjustment logic."""

import pytest

from app.mcp_server.tick_size import adjust_tick_size_kr


class TestKRXTickSizeAdjustment:
    """Test cases for KRX tick size adjustment function."""

    def test_buy_order_rounds_down(self):
        """Buy orders should round DOWN to nearest tick."""
        # Example from task: 327,272원 매수 → 327,000원
        assert adjust_tick_size_kr(327272, "buy") == 327000

        # 2,392,500원 매수 → 2,390,000원
        assert adjust_tick_size_kr(2392500, "buy") == 2390000

        # 15,723원 매수 → 15,720원
        assert adjust_tick_size_kr(15723, "buy") == 15720

        # 49,980원 매수 → 49,950원
        assert adjust_tick_size_kr(49980, "buy") == 49950

    def test_sell_order_rounds_up(self):
        """Sell orders should round UP to nearest tick."""
        # Example from task: 327,272원 매도 → 327,500원
        assert adjust_tick_size_kr(327272, "sell") == 327500

        # Boundary value test - exactly at boundary should round up
        assert adjust_tick_size_kr(2000, "sell") == 2000
        assert adjust_tick_size_kr(5000, "sell") == 5000
        assert adjust_tick_size_kr(20000, "sell") == 20000
        assert adjust_tick_size_kr(50000, "sell") == 50000
        assert adjust_tick_size_kr(200000, "sell") == 200000
        assert adjust_tick_size_kr(500000, "sell") == 500000
        assert adjust_tick_size_kr(1000000, "sell") == 1000000

    def test_boundary_values(self):
        """Test exact boundary values for different price ranges."""
        # ~2,000원 tick size 1
        assert adjust_tick_size_kr(1999, "buy") == 1999
        assert adjust_tick_size_kr(2000, "buy") == 2000
        assert adjust_tick_size_kr(2001, "buy") == 2000
        assert adjust_tick_size_kr(2000, "sell") == 2000

        # 2,000-5,000원 tick size 5
        assert adjust_tick_size_kr(5000, "buy") == 5000
        assert adjust_tick_size_kr(5001, "buy") == 5000
        assert adjust_tick_size_kr(4999, "buy") == 4995

        # 5,000-20,000원 tick size 10
        assert adjust_tick_size_kr(20000, "buy") == 20000
        assert adjust_tick_size_kr(20001, "buy") == 20000
        assert adjust_tick_size_kr(19999, "buy") == 19990

        # 20,000-50,000원 tick size 50
        assert adjust_tick_size_kr(50000, "buy") == 50000
        assert adjust_tick_size_kr(50001, "buy") == 50000
        assert adjust_tick_size_kr(49999, "buy") == 49950

        # 50,000-200,000원 tick size 100
        assert adjust_tick_size_kr(200000, "buy") == 200000
        assert adjust_tick_size_kr(200001, "buy") == 200000
        assert adjust_tick_size_kr(199999, "buy") == 199900

        # 200,000-500,000원 tick size 500
        assert adjust_tick_size_kr(500000, "buy") == 500000
        assert adjust_tick_size_kr(500001, "buy") == 500000
        assert adjust_tick_size_kr(499999, "buy") == 499500

        # 500,000-1,000,000원 tick size 1,000
        assert adjust_tick_size_kr(1000000, "buy") == 1000000
        assert adjust_tick_size_kr(1000001, "buy") == 1000000
        assert adjust_tick_size_kr(999999, "buy") == 999000

        # 1,000,000원~ tick size 5,000
        assert adjust_tick_size_kr(1000000, "sell") == 1000000
        assert adjust_tick_size_kr(1000001, "sell") == 1005000
        assert adjust_tick_size_kr(1500000, "sell") == 1500000
        assert adjust_tick_size_kr(1500001, "sell") == 1505000

    def test_tick_sizes_by_range(self):
        """Verify correct tick size for each price range."""
        # ~2,000원: tick size 1
        assert adjust_tick_size_kr(1000, "buy") == 1000
        assert adjust_tick_size_kr(1500, "buy") == 1500

        # 2,000-5,000원: tick size 5
        assert adjust_tick_size_kr(2500, "buy") == 2500
        assert adjust_tick_size_kr(3000, "buy") == 3000

        # 5,000-20,000원: tick size 10
        assert adjust_tick_size_kr(7500, "buy") == 7500
        assert adjust_tick_size_kr(10000, "buy") == 10000

        # 20,000-50,000원: tick size 50
        assert adjust_tick_size_kr(30000, "buy") == 30000
        assert adjust_tick_size_kr(40000, "buy") == 40000

        # 50,000-200,000원: tick size 100
        assert adjust_tick_size_kr(75000, "buy") == 75000
        assert adjust_tick_size_kr(100000, "buy") == 100000
        assert adjust_tick_size_kr(150000, "buy") == 150000

        # 200,000-500,000원: tick size 500
        assert adjust_tick_size_kr(250000, "buy") == 250000
        assert adjust_tick_size_kr(350000, "buy") == 350000

        # 500,000-1,000,000원: tick size 1,000
        assert adjust_tick_size_kr(750000, "buy") == 750000
        assert adjust_tick_size_kr(900000, "buy") == 900000

        # 1,000,000원~: tick size 5,000
        assert adjust_tick_size_kr(1200000, "buy") == 1200000
        assert adjust_tick_size_kr(2000000, "buy") == 2000000

    def test_invalid_side_raises_error(self):
        """Invalid side parameter should raise ValueError."""
        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            adjust_tick_size_kr(1000, "invalid")

        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            adjust_tick_size_kr(1000, "")

    def test_negative_price_raises_error(self):
        """Negative price should raise ValueError."""
        with pytest.raises(ValueError, match="Price must be non-negative"):
            adjust_tick_size_kr(-100, "buy")

        with pytest.raises(ValueError, match="Price must be non-negative"):
            adjust_tick_size_kr(-1000, "sell")

    def test_minimum_price(self):
        """Price adjustment should ensure minimum of 1 KRW."""
        assert adjust_tick_size_kr(0.5, "buy") == 1
        assert adjust_tick_size_kr(0.1, "sell") == 1

    def test_high_value_prices(self):
        """Test very high prices use largest tick size."""
        # 10 million KRW
        assert adjust_tick_size_kr(10000000, "buy") == 10000000
        # 10.5 million KRW
        assert adjust_tick_size_kr(10500000, "sell") == 10500000
        # 50 million KRW
        assert adjust_tick_size_kr(50000000, "buy") == 50000000
