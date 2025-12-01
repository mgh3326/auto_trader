"""
Trading Integration Tests

수동 잔고와 트레이딩 기능 통합 테스트
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from app.models.manual_holdings import MarketType
from app.services.merged_portfolio_service import (
    MergedPortfolioService,
    ReferencePrices,
    HoldingInfo,
    MergedHolding,
)
from app.services.trading_price_service import (
    TradingPriceService,
    PriceStrategy,
    PriceCalculationResult,
    ExpectedProfit,
)


# =============================================================================
# Unit Tests: MergedPortfolioService
# =============================================================================


@pytest.mark.unit
class TestMergedPortfolioService:
    """MergedPortfolioService 단위 테스트"""

    def test_calculate_combined_avg_basic(self):
        """기본 통합 평단가 계산: 한투 10주@74000 + 토스 5주@73000 = 73667"""
        holdings = [
            HoldingInfo(broker="kis", quantity=10, avg_price=74000),
            HoldingInfo(broker="toss", quantity=5, avg_price=73000),
        ]

        result = MergedPortfolioService.calculate_combined_avg(holdings)

        # 가중 평균: (10 * 74000 + 5 * 73000) / 15 = 1105000 / 15 = 73666.67
        expected = (10 * 74000 + 5 * 73000) / 15
        assert abs(result - expected) < 0.01

    def test_calculate_combined_avg_single_broker(self):
        """단일 브로커만 있을 때"""
        holdings = [
            HoldingInfo(broker="toss", quantity=100, avg_price=50000),
        ]

        result = MergedPortfolioService.calculate_combined_avg(holdings)

        assert result == 50000

    def test_calculate_combined_avg_empty(self):
        """보유 종목이 없을 때"""
        result = MergedPortfolioService.calculate_combined_avg([])

        assert result == 0.0

    def test_calculate_combined_avg_three_brokers(self):
        """3개 브로커 보유 시"""
        holdings = [
            HoldingInfo(broker="kis", quantity=10, avg_price=100),
            HoldingInfo(broker="toss", quantity=20, avg_price=90),
            HoldingInfo(broker="upbit", quantity=30, avg_price=80),
        ]

        result = MergedPortfolioService.calculate_combined_avg(holdings)

        # (10*100 + 20*90 + 30*80) / 60 = (1000 + 1800 + 2400) / 60 = 86.67
        expected = (10 * 100 + 20 * 90 + 30 * 80) / 60
        assert abs(result - expected) < 0.01


@pytest.mark.unit
class TestReferencePrices:
    """ReferencePrices 데이터클래스 테스트"""

    def test_reference_prices_both_brokers(self):
        """한투+토스 둘 다 있을 때 모든 평단가 반환"""
        ref = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73666.67,
            total_quantity=15,
        )

        data = ref.to_dict()

        assert data["kis_avg"] == 74000
        assert data["kis_quantity"] == 10
        assert data["toss_avg"] == 73000
        assert data["toss_quantity"] == 5
        assert abs(data["combined_avg"] - 73666.67) < 0.01
        assert data["total_quantity"] == 15

    def test_reference_prices_toss_only(self):
        """토스만 있을 때 kis_avg는 None"""
        ref = ReferencePrices(
            kis_avg=None,
            kis_quantity=0,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73000,
            total_quantity=5,
        )

        data = ref.to_dict()

        assert data["kis_avg"] is None
        assert data["kis_quantity"] == 0
        assert data["toss_avg"] == 73000
        assert data["toss_quantity"] == 5

    def test_reference_prices_kis_only(self):
        """KIS만 있을 때 toss_avg는 None"""
        ref = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=None,
            toss_quantity=0,
            combined_avg=74000,
            total_quantity=10,
        )

        data = ref.to_dict()

        assert data["kis_avg"] == 74000
        assert data["toss_avg"] is None


# =============================================================================
# Unit Tests: TradingPriceService
# =============================================================================


@pytest.mark.unit
class TestTradingPriceService:
    """TradingPriceService 단위 테스트"""

    def setup_method(self):
        self.service = TradingPriceService()
        self.ref_both = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73666.67,
            total_quantity=15,
        )
        self.ref_toss_only = ReferencePrices(
            kis_avg=None,
            kis_quantity=0,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73000,
            total_quantity=5,
        )

    # --- 매수 가격 계산 테스트 ---

    def test_buy_price_with_combined_avg(self):
        """매수가를 통합 평단가로 설정"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.combined_avg,
        )

        assert abs(result.price - 73666.67) < 0.01
        assert result.price_source == "통합 평단가"

    def test_buy_price_with_kis_avg(self):
        """매수가를 한투 평단가로 설정"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.kis_avg,
        )

        assert result.price == 74000
        assert result.price_source == "한투 평단가"

    def test_buy_price_with_toss_avg(self):
        """매수가를 토스 평단가로 설정"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.toss_avg,
        )

        assert result.price == 73000
        assert result.price_source == "토스 평단가"

    def test_buy_price_with_lowest_avg(self):
        """매수가를 최저 평단가로 설정"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.lowest_avg,
        )

        assert result.price == 73000  # toss_avg가 더 낮음
        assert result.price_source == "최저 평단가"

    def test_buy_price_with_lowest_minus_percent(self):
        """최저 평단가 -1% 계산"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.lowest_minus_percent,
            discount_percent=1.0,
        )

        expected = 73000 * 0.99  # 72270
        assert abs(result.price - expected) < 0.01
        assert "-1.0%" in result.price_source

    def test_buy_price_with_current(self):
        """현재가로 매수"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.current,
        )

        assert result.price == 75000
        assert result.price_source == "현재가"

    def test_buy_price_with_manual(self):
        """직접 입력 가격으로 매수"""
        result = self.service.calculate_buy_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.manual,
            manual_price=72000,
        )

        assert result.price == 72000
        assert result.price_source == "직접 입력"

    def test_buy_price_kis_avg_missing(self):
        """한투 평단가 없을 때 kis_avg 전략 사용 시 에러"""
        with pytest.raises(ValueError, match="한투 평단가 정보가 없습니다"):
            self.service.calculate_buy_price(
                reference_prices=self.ref_toss_only,
                current_price=75000,
                strategy=PriceStrategy.kis_avg,
            )

    # --- 매도 가격 계산 테스트 ---

    def test_sell_price_with_kis_avg_plus(self):
        """한투 평단가 +5% 매도가 계산"""
        result = self.service.calculate_sell_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.kis_avg_plus,
            profit_percent=5.0,
        )

        expected = 74000 * 1.05  # 77700
        assert abs(result.price - expected) < 0.01
        assert "+5.0%" in result.price_source

    def test_sell_price_with_toss_avg_plus(self):
        """토스 평단가 +10% 매도가 계산"""
        result = self.service.calculate_sell_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.toss_avg_plus,
            profit_percent=10.0,
        )

        expected = 73000 * 1.10  # 80300
        assert abs(result.price - expected) < 0.01

    def test_sell_price_with_combined_avg_plus(self):
        """통합 평단가 +5% 매도가 계산"""
        result = self.service.calculate_sell_price(
            reference_prices=self.ref_both,
            current_price=75000,
            strategy=PriceStrategy.combined_avg_plus,
            profit_percent=5.0,
        )

        expected = 73666.67 * 1.05
        assert abs(result.price - expected) < 1  # 소수점 반올림 오차 허용


@pytest.mark.unit
class TestSellQuantityValidation:
    """매도 수량 검증 테스트"""

    def setup_method(self):
        self.service = TradingPriceService()

    def test_sell_within_kis_quantity(self):
        """KIS 보유분 이내 매도 - 성공"""
        is_valid, warning = self.service.validate_sell_quantity(
            kis_quantity=10, requested_quantity=5
        )

        assert is_valid is True
        assert warning is None

    def test_sell_exact_kis_quantity(self):
        """KIS 보유분 전량 매도 - 성공"""
        is_valid, warning = self.service.validate_sell_quantity(
            kis_quantity=10, requested_quantity=10
        )

        assert is_valid is True
        assert warning is None

    def test_sell_exceeds_kis_quantity(self):
        """KIS 보유분 초과 매도 시도 - 에러 발생"""
        is_valid, warning = self.service.validate_sell_quantity(
            kis_quantity=10, requested_quantity=15
        )

        assert is_valid is False
        assert "KIS 보유 수량(10주)을 초과할 수 없습니다" in warning

    def test_sell_when_no_kis_holdings(self):
        """KIS 보유분 0일 때 매도 시도 - 에러 발생"""
        is_valid, warning = self.service.validate_sell_quantity(
            kis_quantity=0, requested_quantity=5
        )

        assert is_valid is False
        assert "KIS 보유분이 없어 매도할 수 없습니다" in warning

    def test_sell_zero_quantity(self):
        """매도 수량 0 - 에러 발생"""
        is_valid, warning = self.service.validate_sell_quantity(
            kis_quantity=10, requested_quantity=0
        )

        assert is_valid is False
        assert "매도 수량은 0보다 커야 합니다" in warning

    def test_sell_negative_quantity(self):
        """매도 수량 음수 - 에러 발생"""
        is_valid, warning = self.service.validate_sell_quantity(
            kis_quantity=10, requested_quantity=-5
        )

        assert is_valid is False
        assert "매도 수량은 0보다 커야 합니다" in warning


@pytest.mark.unit
class TestExpectedProfit:
    """예상 수익 계산 테스트"""

    def setup_method(self):
        self.service = TradingPriceService()
        self.ref_both = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73666.67,
            total_quantity=15,
        )

    def test_expected_profit_calculation(self):
        """예상 수익 계산"""
        result = self.service.calculate_expected_profit(
            quantity=10,
            sell_price=77700,  # 한투 평단가 + 5%
            reference_prices=self.ref_both,
        )

        # 한투 기준: (77700 - 74000) * 10 = 37000
        assert "based_on_kis_avg" in result
        assert result["based_on_kis_avg"].amount == 37000

        # 토스 기준: (77700 - 73000) * 10 = 47000
        assert "based_on_toss_avg" in result
        assert result["based_on_toss_avg"].amount == 47000

        # 통합 기준
        assert "based_on_combined_avg" in result

    def test_expected_profit_percent(self):
        """예상 수익률 계산"""
        result = self.service.calculate_expected_profit(
            quantity=10,
            sell_price=77700,
            reference_prices=self.ref_both,
        )

        # 한투 기준 수익률: (77700 - 74000) / 74000 * 100 = 5%
        expected_percent = (77700 - 74000) / 74000 * 100
        assert abs(result["based_on_kis_avg"].percent - expected_percent) < 0.01


@pytest.mark.unit
class TestGetLowestAvg:
    """최저 평단가 조회 테스트"""

    def test_lowest_avg_both_brokers(self):
        """두 브로커 중 낮은 평단가"""
        ref = ReferencePrices(
            kis_avg=74000,
            toss_avg=73000,
        )

        result = TradingPriceService.get_lowest_avg(ref)

        assert result == 73000

    def test_lowest_avg_kis_lower(self):
        """한투가 더 낮을 때"""
        ref = ReferencePrices(
            kis_avg=72000,
            toss_avg=73000,
        )

        result = TradingPriceService.get_lowest_avg(ref)

        assert result == 72000

    def test_lowest_avg_single_broker(self):
        """단일 브로커만 있을 때"""
        ref = ReferencePrices(
            kis_avg=None,
            toss_avg=73000,
        )

        result = TradingPriceService.get_lowest_avg(ref)

        assert result == 73000

    def test_lowest_avg_no_broker(self):
        """보유 정보 없을 때"""
        ref = ReferencePrices(
            kis_avg=None,
            toss_avg=None,
        )

        result = TradingPriceService.get_lowest_avg(ref)

        assert result is None


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetReferencePricesIntegration:
    """get_reference_prices 통합 테스트 (Mock 사용)"""

    async def test_get_reference_prices_both_brokers(self):
        """한투+토스 둘 다 있을 때 참조 평단가 조회"""
        # Mock 설정
        mock_db = MagicMock()
        mock_manual_service = AsyncMock()

        # 수동 등록 종목 (토스)
        mock_holding = MagicMock()
        mock_holding.broker_account.broker_type.value = "toss"
        mock_holding.quantity = Decimal("5")
        mock_holding.avg_price = Decimal("73000")
        mock_manual_service.get_holdings_by_ticker_all_accounts.return_value = [
            mock_holding
        ]

        service = MergedPortfolioService(mock_db)
        service.manual_holdings_service = mock_manual_service

        # KIS 보유 정보
        kis_holdings = {
            "quantity": 10,
            "avg_price": 74000,
        }

        ref = await service.get_reference_prices(
            user_id=1,
            ticker="005930",
            market_type=MarketType.KR,
            kis_holdings=kis_holdings,
        )

        # 검증
        assert ref.kis_avg == 74000
        assert ref.kis_quantity == 10
        assert ref.toss_avg == 73000
        assert ref.toss_quantity == 5
        assert ref.total_quantity == 15
        # 통합 평단가: (10*74000 + 5*73000) / 15
        expected_combined = (10 * 74000 + 5 * 73000) / 15
        assert abs(ref.combined_avg - expected_combined) < 0.01

    async def test_get_reference_prices_toss_only(self):
        """토스만 있을 때 kis_avg는 None"""
        mock_db = MagicMock()
        mock_manual_service = AsyncMock()

        mock_holding = MagicMock()
        mock_holding.broker_account.broker_type.value = "toss"
        mock_holding.quantity = Decimal("5")
        mock_holding.avg_price = Decimal("73000")
        mock_manual_service.get_holdings_by_ticker_all_accounts.return_value = [
            mock_holding
        ]

        service = MergedPortfolioService(mock_db)
        service.manual_holdings_service = mock_manual_service

        ref = await service.get_reference_prices(
            user_id=1,
            ticker="005930",
            market_type=MarketType.KR,
            kis_holdings=None,  # KIS 보유 없음
        )

        assert ref.kis_avg is None
        assert ref.kis_quantity == 0
        assert ref.toss_avg == 73000
        assert ref.toss_quantity == 5


# =============================================================================
# 요구사항 검증 테스트
# =============================================================================


@pytest.mark.unit
class TestRequirementsVerification:
    """핵심 요구사항 검증 테스트"""

    def setup_method(self):
        self.service = TradingPriceService()

    def test_requirement_buy_uses_all_reference_prices(self):
        """요구사항: 매수 시 한투/토스/통합 평단가 모두 참조 가능"""
        ref = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73666.67,
            total_quantity=15,
        )

        # 한투 평단가로 매수
        r1 = self.service.calculate_buy_price(ref, 75000, PriceStrategy.kis_avg)
        assert r1.price == 74000

        # 토스 평단가로 매수
        r2 = self.service.calculate_buy_price(ref, 75000, PriceStrategy.toss_avg)
        assert r2.price == 73000

        # 통합 평단가로 매수
        r3 = self.service.calculate_buy_price(ref, 75000, PriceStrategy.combined_avg)
        assert abs(r3.price - 73666.67) < 0.01

    def test_requirement_sell_limited_to_kis_quantity(self):
        """요구사항: 매도는 KIS 보유분까지만 가능"""
        # KIS 10주, 토스 5주 -> 매도는 최대 10주까지만
        is_valid, msg = self.service.validate_sell_quantity(
            kis_quantity=10, requested_quantity=11
        )
        assert is_valid is False
        assert "초과" in msg

    def test_requirement_sell_references_all_avg_prices(self):
        """요구사항: 매도 시 한투/토스/통합 평단가 기준 수익 계산"""
        ref = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73666.67,
            total_quantity=15,
        )

        result = self.service.calculate_expected_profit(
            quantity=10,
            sell_price=78000,
            reference_prices=ref,
        )

        # 모든 평단가 기준 예상 수익이 포함되어야 함
        assert "based_on_kis_avg" in result
        assert "based_on_toss_avg" in result
        assert "based_on_combined_avg" in result

    def test_requirement_sell_price_strategies(self):
        """요구사항: 매도 시 각 평단가 +N% 가격 설정 가능"""
        ref = ReferencePrices(
            kis_avg=74000,
            kis_quantity=10,
            toss_avg=73000,
            toss_quantity=5,
            combined_avg=73666.67,
            total_quantity=15,
        )

        # 한투 +5%
        r1 = self.service.calculate_sell_price(
            ref, 75000, PriceStrategy.kis_avg_plus, profit_percent=5
        )
        assert abs(r1.price - 74000 * 1.05) < 0.01

        # 토스 +10%
        r2 = self.service.calculate_sell_price(
            ref, 75000, PriceStrategy.toss_avg_plus, profit_percent=10
        )
        assert abs(r2.price - 73000 * 1.10) < 0.01

        # 통합 +3%
        r3 = self.service.calculate_sell_price(
            ref, 75000, PriceStrategy.combined_avg_plus, profit_percent=3
        )
        assert abs(r3.price - 73666.67 * 1.03) < 1
