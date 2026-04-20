"""Tests for Order Estimation Service"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.order_estimation_service import (
    calculate_estimated_order_cost,
    extract_buy_prices_from_analysis,
    fetch_pending_crypto_buy_cost,
    fetch_pending_domestic_buy_cost,
    fetch_pending_overseas_buy_cost,
)


class TestExtractBuyPrices:
    """extract_buy_prices_from_analysis 테스트"""

    def test_extract_all_four_prices(self):
        """4개 매수 가격 모두 추출"""
        analysis = MagicMock()
        analysis.appropriate_buy_min = Decimal("50000")
        analysis.appropriate_buy_max = Decimal("52000")
        analysis.buy_hope_min = Decimal("48000")
        analysis.buy_hope_max = Decimal("49000")

        result = extract_buy_prices_from_analysis(analysis)

        assert len(result) == 4
        assert result[0] == {"price_name": "appropriate_buy_min", "price": 50000.0}
        assert result[1] == {"price_name": "appropriate_buy_max", "price": 52000.0}
        assert result[2] == {"price_name": "buy_hope_min", "price": 48000.0}
        assert result[3] == {"price_name": "buy_hope_max", "price": 49000.0}

    def test_extract_partial_prices(self):
        """일부 가격만 존재할 때"""
        analysis = MagicMock()
        analysis.appropriate_buy_min = Decimal("50000")
        analysis.appropriate_buy_max = None
        analysis.buy_hope_min = None
        analysis.buy_hope_max = Decimal("49000")

        result = extract_buy_prices_from_analysis(analysis)

        assert len(result) == 2
        assert result[0]["price_name"] == "appropriate_buy_min"
        assert result[1]["price_name"] == "buy_hope_max"

    def test_extract_no_prices(self):
        """가격이 전혀 없을 때"""
        analysis = MagicMock()
        analysis.appropriate_buy_min = None
        analysis.appropriate_buy_max = None
        analysis.buy_hope_min = None
        analysis.buy_hope_max = None

        result = extract_buy_prices_from_analysis(analysis)

        assert result == []


class TestCalculateEstimatedOrderCost:
    """calculate_estimated_order_cost (moved + extended) 테스트"""

    def test_krw_integer_quantity(self):
        """KRW 통화에서 정수 수량"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 50000},
            {"price_name": "buy_hope_min", "price": 48000},
        ]
        result = calculate_estimated_order_cost(
            symbol="005930",
            buy_prices=buy_prices,
            quantity_per_order=2,
            currency="KRW",
        )
        assert result["symbol"] == "005930"
        assert result["total_orders"] == 2
        assert result["total_quantity"] == 4
        assert result["total_cost"] == (50000 * 2) + (48000 * 2)
        assert result["currency"] == "KRW"
        assert result["buy_prices"][0]["quantity"] == 2

    def test_usd_decimal_quantity(self):
        """USD 통화에서 소수점 수량 유지"""
        buy_prices = [{"price_name": "appropriate_buy_min", "price": 150}]
        result = calculate_estimated_order_cost(
            symbol="AAPL",
            buy_prices=buy_prices,
            quantity_per_order=2.5,
            currency="USD",
        )
        assert result["buy_prices"][0]["quantity"] == 2.5
        assert result["total_cost"] == 375.0

    def test_empty_prices(self):
        """빈 가격 목록"""
        result = calculate_estimated_order_cost(
            symbol="BTC", buy_prices=[], quantity_per_order=1, currency="KRW"
        )
        assert result["total_orders"] == 0
        assert result["total_quantity"] == 0
        assert result["total_cost"] == 0

    def test_amount_based_crypto(self):
        """금액 기반 계산 (암호화폐)"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 50_000_000},
            {"price_name": "buy_hope_min", "price": 48_000_000},
        ]
        result = calculate_estimated_order_cost(
            symbol="KRW-BTC",
            buy_prices=buy_prices,
            quantity_per_order=10000,  # 10,000 KRW per order
            currency="KRW",
            amount_based=True,
        )
        assert result["total_orders"] == 2
        assert result["total_cost"] == 20000  # 10000 * 2 prices
        assert result["buy_prices"][0]["cost"] == 10000
        assert result["buy_prices"][0]["quantity"] == pytest.approx(10000 / 50_000_000)

    def test_amount_based_zero_price(self):
        """금액 기반에서 가격이 0일 때 수량 0"""
        buy_prices = [{"price_name": "appropriate_buy_min", "price": 0}]
        result = calculate_estimated_order_cost(
            symbol="KRW-X",
            buy_prices=buy_prices,
            quantity_per_order=10000,
            currency="KRW",
            amount_based=True,
        )
        assert result["buy_prices"][0]["quantity"] == 0
        assert result["buy_prices"][0]["cost"] == 10000


class TestFetchPendingBuyCost:
    """미체결 주문 금액 조회 테스트"""

    @pytest.mark.asyncio
    async def test_fetch_pending_domestic_buy_cost(self):
        """국내 미체결 매수 주문 금액"""
        mock_orders = [
            {"sll_buy_dvsn_cd": "02", "ord_qty": "10", "ord_unpr": "50000"},
            {"sll_buy_dvsn_cd": "01", "ord_qty": "5", "ord_unpr": "60000"},
            {"sll_buy_dvsn_cd": "02", "ord_qty": "3", "ord_unpr": "48000"},
        ]
        with patch("app.services.brokers.kis.client.KISClient") as MockKIS:
            mock_instance = AsyncMock()
            mock_instance.inquire_korea_orders.return_value = mock_orders
            MockKIS.return_value = mock_instance

            result = await fetch_pending_domestic_buy_cost()

            assert result == (10 * 50000) + (3 * 48000)

    @pytest.mark.asyncio
    async def test_fetch_pending_domestic_buy_cost_error(self):
        """국내 미체결 조회 실패 시 0 반환"""
        with patch("app.services.brokers.kis.client.KISClient") as MockKIS:
            mock_instance = AsyncMock()
            mock_instance.inquire_korea_orders.side_effect = Exception("API Error")
            MockKIS.return_value = mock_instance

            result = await fetch_pending_domestic_buy_cost()

            assert result == 0.0

    @pytest.mark.asyncio
    async def test_fetch_pending_overseas_buy_cost(self):
        """해외 미체결 매수 주문 금액"""
        mock_orders = [
            {"sll_buy_dvsn_cd": "02", "ft_ord_qty": "5", "ft_ord_unpr3": "150.50"},
            {"sll_buy_dvsn_cd": "01", "ft_ord_qty": "3", "ft_ord_unpr3": "200.00"},
        ]
        with patch("app.services.brokers.kis.client.KISClient") as MockKIS:
            mock_instance = AsyncMock()
            mock_instance.inquire_overseas_orders.return_value = mock_orders
            MockKIS.return_value = mock_instance

            result = await fetch_pending_overseas_buy_cost()

            assert result == 5 * 150.50

    @pytest.mark.asyncio
    async def test_fetch_pending_crypto_buy_cost_limit_order(self):
        """암호화폐 미체결 지정가 매수 주문"""
        mock_orders = [
            {
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000",
                "remaining_volume": "0.001",
            },
        ]
        with patch(
            "app.services.brokers.upbit.client.fetch_open_orders",
            new_callable=AsyncMock,
            return_value=mock_orders,
        ):
            result = await fetch_pending_crypto_buy_cost()

            assert result == 50000000 * 0.001

    @pytest.mark.asyncio
    async def test_fetch_pending_crypto_buy_cost_market_order(self):
        """암호화폐 미체결 시장가 매수 주문"""
        mock_orders = [
            {"side": "bid", "ord_type": "price", "price": "100000"},
        ]
        with patch(
            "app.services.brokers.upbit.client.fetch_open_orders",
            new_callable=AsyncMock,
            return_value=mock_orders,
        ):
            result = await fetch_pending_crypto_buy_cost()

            assert result == 100000.0
