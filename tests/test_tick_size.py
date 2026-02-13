"""Tests for KRX tick size adjustment logic."""

import pytest

from app.mcp_server.tick_size import adjust_tick_size_kr, get_tick_size_kr


class TestGetTickSizeKR:
    """Test cases for get_tick_size_kr function (KRX 2023+ rules)."""

    def test_tick_size_below_2000(self):
        assert get_tick_size_kr(1000) == 1
        assert get_tick_size_kr(1999) == 1

    def test_tick_size_2000_to_5000(self):
        assert get_tick_size_kr(2000) == 5
        assert get_tick_size_kr(3000) == 5
        assert get_tick_size_kr(4999) == 5

    def test_tick_size_5000_to_20000(self):
        assert get_tick_size_kr(5000) == 10
        assert get_tick_size_kr(10000) == 10
        assert get_tick_size_kr(19999) == 10

    def test_tick_size_20000_to_50000(self):
        assert get_tick_size_kr(20000) == 50
        assert get_tick_size_kr(35000) == 50
        assert get_tick_size_kr(49999) == 50

    def test_tick_size_50000_to_200000(self):
        assert get_tick_size_kr(50000) == 100
        assert get_tick_size_kr(100000) == 100
        assert get_tick_size_kr(199999) == 100

    def test_tick_size_200000_to_500000(self):
        assert get_tick_size_kr(200000) == 500
        assert get_tick_size_kr(350000) == 500
        assert get_tick_size_kr(499999) == 500

    def test_tick_size_500000_and_above(self):
        assert get_tick_size_kr(500000) == 1000
        assert get_tick_size_kr(1000000) == 1000
        assert get_tick_size_kr(10000000) == 1000


class TestKRXTickSizeAdjustment:
    """Test cases for KRX tick size adjustment function."""

    def test_buy_order_rounds_down(self):
        assert adjust_tick_size_kr(327272, "buy") == 327000
        assert adjust_tick_size_kr(2392500, "buy") == 2392000
        assert adjust_tick_size_kr(15723, "buy") == 15720
        assert adjust_tick_size_kr(49980, "buy") == 49950

    def test_sell_order_rounds_up(self):
        assert adjust_tick_size_kr(327272, "sell") == 327500
        assert adjust_tick_size_kr(2000, "sell") == 2000
        assert adjust_tick_size_kr(5000, "sell") == 5000
        assert adjust_tick_size_kr(20000, "sell") == 20000
        assert adjust_tick_size_kr(50000, "sell") == 50000
        assert adjust_tick_size_kr(200000, "sell") == 200000
        assert adjust_tick_size_kr(500000, "sell") == 500000
        assert adjust_tick_size_kr(1000000, "sell") == 1000000

    def test_boundary_values(self):
        assert adjust_tick_size_kr(1999, "buy") == 1999
        assert adjust_tick_size_kr(2000, "buy") == 2000
        assert adjust_tick_size_kr(2001, "buy") == 2000
        assert adjust_tick_size_kr(2000, "sell") == 2000

        assert adjust_tick_size_kr(5000, "buy") == 5000
        assert adjust_tick_size_kr(5001, "buy") == 5000
        assert adjust_tick_size_kr(4999, "buy") == 4995

        assert adjust_tick_size_kr(20000, "buy") == 20000
        assert adjust_tick_size_kr(20001, "buy") == 20000
        assert adjust_tick_size_kr(19999, "buy") == 19990

        assert adjust_tick_size_kr(50000, "buy") == 50000
        assert adjust_tick_size_kr(50001, "buy") == 50000
        assert adjust_tick_size_kr(49999, "buy") == 49950

        assert adjust_tick_size_kr(200000, "buy") == 200000
        assert adjust_tick_size_kr(200001, "buy") == 200000
        assert adjust_tick_size_kr(199999, "buy") == 199900

        assert adjust_tick_size_kr(500000, "buy") == 500000
        assert adjust_tick_size_kr(500001, "buy") == 500000
        assert adjust_tick_size_kr(499999, "buy") == 499500

        assert adjust_tick_size_kr(1000000, "buy") == 1000000
        assert adjust_tick_size_kr(1000001, "buy") == 1000000
        assert adjust_tick_size_kr(999999, "buy") == 999000

        assert adjust_tick_size_kr(1000000, "sell") == 1000000
        assert adjust_tick_size_kr(1000001, "sell") == 1001000
        assert adjust_tick_size_kr(1500000, "sell") == 1500000
        assert adjust_tick_size_kr(1500001, "sell") == 1501000

    def test_tick_sizes_by_range(self):
        assert adjust_tick_size_kr(1000, "buy") == 1000
        assert adjust_tick_size_kr(1500, "buy") == 1500

        assert adjust_tick_size_kr(2500, "buy") == 2500
        assert adjust_tick_size_kr(3000, "buy") == 3000

        assert adjust_tick_size_kr(7500, "buy") == 7500
        assert adjust_tick_size_kr(10000, "buy") == 10000

        assert adjust_tick_size_kr(30000, "buy") == 30000
        assert adjust_tick_size_kr(40000, "buy") == 40000

        assert adjust_tick_size_kr(75000, "buy") == 75000
        assert adjust_tick_size_kr(100000, "buy") == 100000
        assert adjust_tick_size_kr(150000, "buy") == 150000

        assert adjust_tick_size_kr(250000, "buy") == 250000
        assert adjust_tick_size_kr(350000, "buy") == 350000

        assert adjust_tick_size_kr(750000, "buy") == 750000
        assert adjust_tick_size_kr(900000, "buy") == 900000

        assert adjust_tick_size_kr(1200000, "buy") == 1200000
        assert adjust_tick_size_kr(2000000, "buy") == 2000000

    def test_invalid_side_raises_error(self):
        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            adjust_tick_size_kr(1000, "invalid")

        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            adjust_tick_size_kr(1000, "")

    def test_negative_price_raises_error(self):
        with pytest.raises(ValueError, match="Price must be non-negative"):
            adjust_tick_size_kr(-100, "buy")

        with pytest.raises(ValueError, match="Price must be non-negative"):
            adjust_tick_size_kr(-1000, "sell")

    def test_minimum_price(self):
        assert adjust_tick_size_kr(0.5, "buy") == 1
        assert adjust_tick_size_kr(0.1, "sell") == 1

    def test_high_value_prices(self):
        assert adjust_tick_size_kr(10000000, "buy") == 10000000
        assert adjust_tick_size_kr(10500000, "sell") == 10500000
        assert adjust_tick_size_kr(50000000, "buy") == 50000000


class TestPR139Regression:
    """Test cases for PR #139 regression fix (KRX 2023+ rules).

    Verify that prices >= 500,000 use 1,000 tick size (not 5,000).
    """

    def test_no_adjustment_valid_prices(self):
        assert adjust_tick_size_kr(1_098_000, "buy") == 1_098_000
        assert adjust_tick_size_kr(312_000, "buy") == 312_000
        assert adjust_tick_size_kr(352_000, "buy") == 352_000
        assert adjust_tick_size_kr(50_100, "buy") == 50_100

    def test_adjustment_required_buy(self):
        assert adjust_tick_size_kr(1_098_500, "buy") == 1_098_000
        assert adjust_tick_size_kr(312_300, "buy") == 312_000

    def test_adjustment_required_sell(self):
        assert adjust_tick_size_kr(312_300, "sell") == 312_500

    def test_boundary_500000(self):
        assert adjust_tick_size_kr(500_000, "buy") == 500_000
        assert adjust_tick_size_kr(499_999, "buy") == 499_500
        assert adjust_tick_size_kr(200_000, "buy") == 200_000

    def test_million_plus_prices_use_1000_tick(self):
        assert get_tick_size_kr(1_000_000) == 1000
        assert get_tick_size_kr(1_098_000) == 1000
        assert get_tick_size_kr(2_392_500) == 1000
        assert get_tick_size_kr(10_000_000) == 1000

    def test_million_plus_adjustment(self):
        assert adjust_tick_size_kr(1_098_500, "buy") == 1_098_000
        assert adjust_tick_size_kr(1_098_500, "sell") == 1_099_000
        assert adjust_tick_size_kr(2_392_500, "buy") == 2_392_000
        assert adjust_tick_size_kr(2_392_500, "sell") == 2_393_000
