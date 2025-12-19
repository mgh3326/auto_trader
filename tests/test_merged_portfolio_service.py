"""
Tests for MergedPortfolioService - 통합 포트폴리오 서비스 테스트
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.models.manual_holdings import MarketType
from app.services.merged_portfolio_service import (
    HoldingInfo,
    MergedHolding,
    MergedPortfolioService,
    ReferencePrices,
)


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_kis_client():
    """Mock KIS client."""
    client = AsyncMock()
    client.fetch_my_stocks = AsyncMock(return_value=[])
    client.fetch_my_overseas_stocks = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_manual_holdings_service():
    """Mock ManualHoldingsService."""
    return AsyncMock()


@pytest.fixture
def merged_portfolio_service(mock_db_session):
    """Create MergedPortfolioService instance with mocked dependencies."""
    service = MergedPortfolioService(mock_db_session)
    return service


class TestCalculateCombinedAvg:
    """가중 평균 평단가 계산 테스트"""

    def test_single_holding(self):
        """단일 보유 정보의 평단가"""
        holdings = [HoldingInfo(broker="kis", quantity=100, avg_price=50000)]
        result = MergedPortfolioService.calculate_combined_avg(holdings)
        assert result == 50000.0

    def test_multiple_holdings_same_avg(self):
        """동일 평단가 다중 보유"""
        holdings = [
            HoldingInfo(broker="kis", quantity=100, avg_price=50000),
            HoldingInfo(broker="toss", quantity=100, avg_price=50000),
        ]
        result = MergedPortfolioService.calculate_combined_avg(holdings)
        assert result == 50000.0

    def test_multiple_holdings_different_avg(self):
        """다른 평단가 다중 보유 - 가중 평균 계산"""
        holdings = [
            HoldingInfo(broker="kis", quantity=100, avg_price=40000),  # 4,000,000
            HoldingInfo(broker="toss", quantity=200, avg_price=50000),  # 10,000,000
        ]
        # 총 금액: 14,000,000 / 총 수량: 300 = 46,666.67
        result = MergedPortfolioService.calculate_combined_avg(holdings)
        assert round(result, 2) == 46666.67

    def test_empty_holdings(self):
        """빈 보유 목록"""
        result = MergedPortfolioService.calculate_combined_avg([])
        assert result == 0.0

    def test_zero_quantity(self):
        """수량이 0인 경우"""
        holdings = [HoldingInfo(broker="kis", quantity=0, avg_price=50000)]
        result = MergedPortfolioService.calculate_combined_avg(holdings)
        assert result == 0.0


class TestGetOrCreateHolding:
    """_get_or_create_holding 테스트"""

    def test_create_new_holding(self):
        """새 종목 생성"""
        merged = {}
        holding = MergedPortfolioService._get_or_create_holding(
            merged, "005930", "삼성전자", MarketType.KR, 77800.0
        )
        assert holding.ticker == "005930"
        assert holding.name == "삼성전자"
        assert holding.market_type == "KR"
        assert holding.current_price == 77800.0
        assert "005930" in merged

    def test_get_existing_holding(self):
        """기존 종목 조회"""
        existing = MergedHolding(
            ticker="005930",
            name="삼성전자",
            market_type="KR",
            current_price=77000.0,
        )
        merged = {"005930": existing}

        holding = MergedPortfolioService._get_or_create_holding(
            merged, "005930", "삼성전자", MarketType.KR, 77800.0
        )
        assert holding is existing
        assert holding.current_price == 77800.0  # 현재가 업데이트

    def test_get_existing_holding_no_price_update(self):
        """기존 종목 조회 - 현재가 미전달 시 유지"""
        existing = MergedHolding(
            ticker="005930",
            name="삼성전자",
            market_type="KR",
            current_price=77000.0,
        )
        merged = {"005930": existing}

        holding = MergedPortfolioService._get_or_create_holding(
            merged, "005930", "삼성전자", MarketType.KR
        )
        assert holding.current_price == 77000.0  # 현재가 유지


class TestApplyKISHoldings:
    """_apply_kis_holdings 테스트"""

    def test_apply_kr_holdings(self, merged_portfolio_service):
        """국내주식 KIS 보유 종목 적용"""
        merged = {}
        stocks = [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "hldg_qty": "100",
                "pchs_avg_pric": "70000",
                "prpr": "77800",
                "evlu_amt": "7780000",
                "evlu_pfls_amt": "780000",
                "evlu_pfls_rt": "1114",  # 11.14%
            }
        ]

        merged_portfolio_service._apply_kis_holdings(merged, stocks, MarketType.KR)

        assert "005930" in merged
        holding = merged["005930"]
        assert holding.name == "삼성전자"
        assert holding.kis_quantity == 100
        assert holding.kis_avg_price == 70000.0
        assert holding.current_price == 77800.0
        assert holding.evaluation == 7780000.0
        assert holding.profit_loss == 780000.0
        assert holding.profit_rate == 11.14
        assert len(holding.holdings) == 1
        assert holding.holdings[0].broker == "kis"

    def test_apply_us_holdings(self, merged_portfolio_service):
        """해외주식 KIS 보유 종목 적용"""
        merged = {}
        stocks = [
            {
                "ovrs_pdno": "AAPL",
                "ovrs_item_name": "Apple Inc",
                "ovrs_cblc_qty": "10",
                "pchs_avg_pric": "150.00",
                "now_pric2": "175.00",
                "ovrs_stck_evlu_amt": "1750.00",
                "frcr_evlu_pfls_amt": "250.00",
                "evlu_pfls_rt": "1667",  # 16.67%
            }
        ]

        merged_portfolio_service._apply_kis_holdings(merged, stocks, MarketType.US)

        assert "AAPL" in merged
        holding = merged["AAPL"]
        assert holding.name == "Apple Inc"
        assert holding.kis_quantity == 10
        assert holding.current_price == 175.0
        assert holding.profit_rate == 16.67


class TestApplyManualHoldings:
    """_apply_manual_holdings 테스트"""

    @pytest.mark.asyncio
    async def test_apply_toss_holdings_new_stock(self, merged_portfolio_service):
        """TOSS 보유 종목 - 새 종목 추가"""
        merged = {}

        # Mock ManualHolding
        mock_holding = MagicMock()
        mock_holding.ticker = "005380"
        mock_holding.quantity = Decimal("50")
        mock_holding.avg_price = Decimal("220000")
        mock_holding.display_name = "현대차"
        mock_holding.broker_account = MagicMock()
        mock_holding.broker_account.broker_type = MagicMock(value="toss")

        merged_portfolio_service.manual_holdings_service.get_holdings_by_user = (
            AsyncMock(return_value=[mock_holding])
        )

        await merged_portfolio_service._apply_manual_holdings(
            merged, user_id=1, market_type=MarketType.KR
        )

        assert "005380" in merged
        holding = merged["005380"]
        assert holding.name == "현대차"
        assert holding.toss_quantity == 50
        assert holding.toss_avg_price == 220000.0
        assert len(holding.holdings) == 1
        assert holding.holdings[0].broker == "toss"

    @pytest.mark.asyncio
    async def test_apply_toss_holdings_existing_stock(self, merged_portfolio_service):
        """TOSS 보유 종목 - 기존 KIS 종목에 추가"""
        existing = MergedHolding(
            ticker="005930",
            name="삼성전자",
            market_type="KR",
            current_price=77800.0,
            kis_quantity=100,
            kis_avg_price=70000.0,
            holdings=[HoldingInfo(broker="kis", quantity=100, avg_price=70000.0)],
        )
        merged = {"005930": existing}

        mock_holding = MagicMock()
        mock_holding.ticker = "005930"
        mock_holding.quantity = Decimal("50")
        mock_holding.avg_price = Decimal("75000")
        mock_holding.display_name = "삼성전자"
        mock_holding.broker_account = MagicMock()
        mock_holding.broker_account.broker_type = MagicMock(value="toss")

        merged_portfolio_service.manual_holdings_service.get_holdings_by_user = (
            AsyncMock(return_value=[mock_holding])
        )

        await merged_portfolio_service._apply_manual_holdings(
            merged, user_id=1, market_type=MarketType.KR
        )

        holding = merged["005930"]
        assert holding.kis_quantity == 100
        assert holding.toss_quantity == 50
        assert len(holding.holdings) == 2


class TestFetchMissingPrices:
    """_fetch_missing_prices 테스트 - TOSS 전용 종목 현재가 조회"""

    @pytest.mark.asyncio
    async def test_fetch_price_for_toss_only_stock(
        self, merged_portfolio_service, mock_kis_client
    ):
        """TOSS만 보유한 종목의 현재가 조회"""
        # 현재가가 0인 TOSS 전용 종목
        merged = {
            "005380": MergedHolding(
                ticker="005380",
                name="현대차",
                market_type="KR",
                current_price=0.0,
                total_quantity=50,
                toss_quantity=50,
                toss_avg_price=220000.0,
                holdings=[HoldingInfo(broker="toss", quantity=50, avg_price=220000.0)],
            )
        }

        # KIS API 현재가 응답 Mock
        price_df = pd.DataFrame([{"close": 230000.0}])
        mock_kis_client.inquire_price = AsyncMock(return_value=price_df)

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.KR, mock_kis_client
        )

        assert merged["005380"].current_price == 230000.0
        mock_kis_client.inquire_price.assert_called_once_with("005380")

    @pytest.mark.asyncio
    async def test_skip_stocks_with_price(
        self, merged_portfolio_service, mock_kis_client
    ):
        """현재가가 이미 있는 종목은 조회하지 않음"""
        merged = {
            "005930": MergedHolding(
                ticker="005930",
                name="삼성전자",
                market_type="KR",
                current_price=77800.0,  # 이미 현재가 있음
                total_quantity=100,
            )
        }

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.KR, mock_kis_client
        )

        mock_kis_client.inquire_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_zero_quantity_stocks(
        self, merged_portfolio_service, mock_kis_client
    ):
        """수량이 0인 종목은 조회하지 않음"""
        merged = {
            "005380": MergedHolding(
                ticker="005380",
                name="현대차",
                market_type="KR",
                current_price=0.0,
                total_quantity=0,  # 수량 없음
            )
        }

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.KR, mock_kis_client
        )

        mock_kis_client.inquire_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_api_error_gracefully(
        self, merged_portfolio_service, mock_kis_client
    ):
        """API 오류 시 로그만 남기고 계속 진행"""
        merged = {
            "005380": MergedHolding(
                ticker="005380",
                name="현대차",
                market_type="KR",
                current_price=0.0,
                total_quantity=50,
            )
        }

        mock_kis_client.inquire_price = AsyncMock(side_effect=Exception("API Error"))

        # 예외 발생해도 에러 없이 진행
        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.KR, mock_kis_client
        )

        # 현재가는 0으로 유지
        assert merged["005380"].current_price == 0.0

    @pytest.mark.asyncio
    async def test_fetch_multiple_missing_prices(
        self, merged_portfolio_service, mock_kis_client
    ):
        """여러 TOSS 전용 종목의 현재가 조회"""
        merged = {
            "005380": MergedHolding(
                ticker="005380",
                name="현대차",
                market_type="KR",
                current_price=0.0,
                total_quantity=50,
            ),
            "010140": MergedHolding(
                ticker="010140",
                name="삼성중공업",
                market_type="KR",
                current_price=0.0,
                total_quantity=100,
            ),
        }

        # 각 종목별 현재가 응답
        def mock_inquire_price(ticker):
            prices = {"005380": 230000.0, "010140": 21800.0}
            return pd.DataFrame([{"close": prices[ticker]}])

        mock_kis_client.inquire_price = AsyncMock(side_effect=mock_inquire_price)

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.KR, mock_kis_client
        )

        assert merged["005380"].current_price == 230000.0
        assert merged["010140"].current_price == 21800.0
        assert mock_kis_client.inquire_price.call_count == 2


class TestFinalizeHoldings:
    """_finalize_holdings 테스트"""

    def test_calculate_total_quantity(self, merged_portfolio_service):
        """총 수량 계산"""
        merged = {
            "005930": MergedHolding(
                ticker="005930",
                name="삼성전자",
                market_type="KR",
                current_price=77800.0,
                holdings=[
                    HoldingInfo(broker="kis", quantity=100, avg_price=70000),
                    HoldingInfo(broker="toss", quantity=50, avg_price=75000),
                ],
            )
        }

        merged_portfolio_service._finalize_holdings(merged)

        holding = merged["005930"]
        assert holding.total_quantity == 150

    def test_calculate_combined_avg_price(self, merged_portfolio_service):
        """통합 평단가 계산"""
        merged = {
            "005930": MergedHolding(
                ticker="005930",
                name="삼성전자",
                market_type="KR",
                current_price=77800.0,
                holdings=[
                    HoldingInfo(broker="kis", quantity=100, avg_price=70000),
                    HoldingInfo(broker="toss", quantity=50, avg_price=76000),
                ],
            )
        }

        merged_portfolio_service._finalize_holdings(merged)

        holding = merged["005930"]
        # (100 * 70000 + 50 * 76000) / 150 = 10,800,000 / 150 = 72000
        assert holding.combined_avg_price == 72000.0

    def test_calculate_evaluation_and_profit(self, merged_portfolio_service):
        """평가금액 및 손익 계산"""
        merged = {
            "005930": MergedHolding(
                ticker="005930",
                name="삼성전자",
                market_type="KR",
                current_price=77800.0,
                holdings=[
                    HoldingInfo(broker="kis", quantity=100, avg_price=70000),
                    HoldingInfo(broker="toss", quantity=50, avg_price=76000),
                ],
            )
        }

        merged_portfolio_service._finalize_holdings(merged)

        holding = merged["005930"]
        # 평가금액: 77800 * 150 = 11,670,000
        assert holding.evaluation == 11670000.0
        # 손익: (77800 - 72000) * 150 = 870,000
        assert holding.profit_loss == 870000.0
        # 수익률: (77800 - 72000) / 72000 = 0.0806
        assert round(holding.profit_rate, 4) == 0.0806

    def test_skip_calculation_without_price(self, merged_portfolio_service):
        """현재가가 없으면 평가금액 계산하지 않음"""
        merged = {
            "005380": MergedHolding(
                ticker="005380",
                name="현대차",
                market_type="KR",
                current_price=0.0,  # 현재가 없음
                holdings=[
                    HoldingInfo(broker="toss", quantity=50, avg_price=220000),
                ],
            )
        }

        merged_portfolio_service._finalize_holdings(merged)

        holding = merged["005380"]
        assert holding.evaluation == 0.0
        assert holding.profit_loss == 0.0
        assert holding.profit_rate == 0.0


class TestBuildMergedPortfolio:
    """_build_merged_portfolio 통합 테스트"""

    @pytest.mark.asyncio
    async def test_toss_only_stock_gets_current_price(
        self, merged_portfolio_service, mock_kis_client
    ):
        """TOSS만 있는 종목이 현재가를 정상 조회하는지 확인"""
        # KIS 보유 종목 없음
        mock_kis_client.fetch_my_stocks = AsyncMock(return_value=[])

        # TOSS 보유 종목
        mock_holding = MagicMock()
        mock_holding.ticker = "005380"
        mock_holding.quantity = Decimal("50")
        mock_holding.avg_price = Decimal("220000")
        mock_holding.display_name = "현대차"
        mock_holding.broker_account = MagicMock()
        mock_holding.broker_account.broker_type = MagicMock(value="toss")

        merged_portfolio_service.manual_holdings_service.get_holdings_by_user = (
            AsyncMock(return_value=[mock_holding])
        )

        # KIS 현재가 조회 응답
        price_df = pd.DataFrame([{"close": 230000.0}])
        mock_kis_client.inquire_price = AsyncMock(return_value=price_df)

        # 분석/설정 조회 Mock
        with patch.object(
            merged_portfolio_service, "_attach_analysis_and_settings", AsyncMock()
        ):
            result = await merged_portfolio_service._build_merged_portfolio(
                user_id=1, market_type=MarketType.KR, kis_client=mock_kis_client
            )

        assert len(result) == 1
        holding = result[0]
        assert holding.ticker == "005380"
        assert holding.current_price == 230000.0
        assert holding.toss_quantity == 50
        assert holding.total_quantity == 50
        # 평가금액: 230000 * 50 = 11,500,000
        assert holding.evaluation == 11500000.0
        # 손익: (230000 - 220000) * 50 = 500,000
        assert holding.profit_loss == 500000.0

    @pytest.mark.asyncio
    async def test_mixed_kis_and_toss_holdings(
        self, merged_portfolio_service, mock_kis_client
    ):
        """KIS와 TOSS 혼합 보유 종목"""
        # KIS 보유 종목 (삼성전자만)
        kis_stocks = [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "hldg_qty": "100",
                "pchs_avg_pric": "70000",
                "prpr": "77800",
                "evlu_amt": "7780000",
                "evlu_pfls_amt": "780000",
                "evlu_pfls_rt": "1114",
            }
        ]
        mock_kis_client.fetch_my_stocks = AsyncMock(return_value=kis_stocks)

        # TOSS 보유 종목 (현대차만 - KIS에 없음)
        mock_holding = MagicMock()
        mock_holding.ticker = "005380"
        mock_holding.quantity = Decimal("50")
        mock_holding.avg_price = Decimal("220000")
        mock_holding.display_name = "현대차"
        mock_holding.broker_account = MagicMock()
        mock_holding.broker_account.broker_type = MagicMock(value="toss")

        merged_portfolio_service.manual_holdings_service.get_holdings_by_user = (
            AsyncMock(return_value=[mock_holding])
        )

        # 현대차 현재가 조회 응답
        price_df = pd.DataFrame([{"close": 230000.0}])
        mock_kis_client.inquire_price = AsyncMock(return_value=price_df)

        with patch.object(
            merged_portfolio_service, "_attach_analysis_and_settings", AsyncMock()
        ):
            result = await merged_portfolio_service._build_merged_portfolio(
                user_id=1, market_type=MarketType.KR, kis_client=mock_kis_client
            )

        assert len(result) == 2

        # 삼성전자 - KIS에서 현재가 제공됨
        samsung = next(h for h in result if h.ticker == "005930")
        assert samsung.current_price == 77800.0
        assert samsung.kis_quantity == 100

        # 현대차 - KIS API로 현재가 조회됨
        hyundai = next(h for h in result if h.ticker == "005380")
        assert hyundai.current_price == 230000.0
        assert hyundai.toss_quantity == 50


class TestReferencePrices:
    """ReferencePrices 테스트"""

    def test_reference_prices_to_dict(self):
        """ReferencePrices to_dict 변환"""
        ref = ReferencePrices(
            kis_avg=70000.0,
            kis_quantity=100,
            toss_avg=75000.0,
            toss_quantity=50,
            combined_avg=71666.67,
            total_quantity=150,
        )

        result = ref.to_dict()

        assert result["kis_avg"] == 70000.0
        assert result["kis_quantity"] == 100
        assert result["toss_avg"] == 75000.0
        assert result["toss_quantity"] == 50
        assert result["combined_avg"] == 71666.67
        assert result["total_quantity"] == 150


class TestMergedHoldingToDict:
    """MergedHolding to_dict 테스트"""

    def test_to_dict_with_all_fields(self):
        """모든 필드가 포함된 to_dict"""
        holding = MergedHolding(
            ticker="005930",
            name="삼성전자",
            market_type="KR",
            holdings=[HoldingInfo(broker="kis", quantity=100, avg_price=70000)],
            kis_quantity=100,
            kis_avg_price=70000.0,
            toss_quantity=50,
            toss_avg_price=75000.0,
            combined_avg_price=71666.67,
            total_quantity=150,
            current_price=77800.0,
            evaluation=11670000.0,
            profit_loss=920000.0,
            profit_rate=0.0855,
            analysis_id=123,
            last_analysis_at="2024-01-01T12:00:00",
            last_analysis_decision="hold",
            analysis_confidence=85,
        )

        result = holding.to_dict()

        assert result["ticker"] == "005930"
        assert result["name"] == "삼성전자"
        assert result["current_price"] == 77800.0
        assert result["evaluation"] == 11670000.0
        assert result["analysis_id"] == 123
        assert len(result["holdings"]) == 1


class TestFetchMissingPricesOverseas:
    """해외주식 _fetch_missing_prices 테스트 - TOSS 전용 해외 종목 현재가 조회"""

    @pytest.mark.asyncio
    async def test_fetch_overseas_price_for_toss_only_stock(
        self, merged_portfolio_service, mock_kis_client
    ):
        """TOSS만 보유한 해외 종목의 현재가 조회"""
        # 현재가가 0인 TOSS 전용 해외 종목
        merged = {
            "CONY": MergedHolding(
                ticker="CONY",
                name="CONY",
                market_type="US",
                current_price=0.0,
                total_quantity=20,
                toss_quantity=20,
                toss_avg_price=17.18,
                holdings=[HoldingInfo(broker="toss", quantity=20, avg_price=17.18)],
            )
        }

        # KIS API 해외주식 현재가 응답 Mock
        price_df = pd.DataFrame([{"close": 18.50}])
        mock_kis_client.inquire_overseas_price = AsyncMock(return_value=price_df)

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.US, mock_kis_client
        )

        assert merged["CONY"].current_price == 18.50
        mock_kis_client.inquire_overseas_price.assert_called_once_with("CONY")

    @pytest.mark.asyncio
    async def test_fetch_overseas_price_multiple_stocks(
        self, merged_portfolio_service, mock_kis_client
    ):
        """여러 TOSS 전용 해외 종목의 현재가 조회"""
        merged = {
            "CONY": MergedHolding(
                ticker="CONY",
                name="CONY",
                market_type="US",
                current_price=0.0,
                total_quantity=20,
            ),
            "BRK-B": MergedHolding(
                ticker="BRK-B",
                name="버크셔 해서웨이 B",
                market_type="US",
                current_price=0.0,
                total_quantity=5,
            ),
        }

        # 각 종목별 현재가 응답
        def mock_inquire_overseas_price(ticker):
            prices = {"CONY": 18.50, "BRK-B": 474.17}
            return pd.DataFrame([{"close": prices[ticker]}])

        mock_kis_client.inquire_overseas_price = AsyncMock(
            side_effect=mock_inquire_overseas_price
        )

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.US, mock_kis_client
        )

        assert merged["CONY"].current_price == 18.50
        assert merged["BRK-B"].current_price == 474.17
        assert mock_kis_client.inquire_overseas_price.call_count == 2

    @pytest.mark.asyncio
    async def test_skip_overseas_stocks_with_price(
        self, merged_portfolio_service, mock_kis_client
    ):
        """현재가가 이미 있는 해외 종목은 조회하지 않음"""
        merged = {
            "TSLA": MergedHolding(
                ticker="TSLA",
                name="Tesla",
                market_type="US",
                current_price=250.0,  # 이미 현재가 있음
                total_quantity=3,
            )
        }

        mock_kis_client.inquire_overseas_price = AsyncMock()

        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.US, mock_kis_client
        )

        mock_kis_client.inquire_overseas_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_overseas_api_error_gracefully(
        self, merged_portfolio_service, mock_kis_client
    ):
        """해외주식 API 오류 시 로그만 남기고 계속 진행"""
        merged = {
            "INVALID": MergedHolding(
                ticker="INVALID",
                name="존재하지 않는 종목",
                market_type="US",
                current_price=0.0,
                total_quantity=10,
            )
        }

        mock_kis_client.inquire_overseas_price = AsyncMock(
            side_effect=Exception("API Error: 해당종목정보가 없습니다")
        )

        # 예외 발생해도 에러 없이 진행
        await merged_portfolio_service._fetch_missing_prices(
            merged, MarketType.US, mock_kis_client
        )

        # 현재가는 0으로 유지
        assert merged["INVALID"].current_price == 0.0
