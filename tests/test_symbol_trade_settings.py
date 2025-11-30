"""
Tests for Symbol Trade Settings functionality
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.symbol_trade_settings import SymbolTradeSettings
from app.models.trading import InstrumentType
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    calculate_estimated_order_cost,
    get_buy_quantity_for_symbol,
    get_buy_quantity_for_crypto,
)


class TestCalculateEstimatedOrderCost:
    """calculate_estimated_order_cost 함수 테스트"""

    def test_basic_calculation(self):
        """기본 비용 계산 테스트"""
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
        assert result["quantity_per_order"] == 2
        assert result["total_orders"] == 2
        assert result["total_quantity"] == 4
        assert result["total_cost"] == (50000 * 2) + (48000 * 2)
        assert result["currency"] == "KRW"

    def test_single_price(self):
        """단일 가격 테스트"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 100},
        ]

        result = calculate_estimated_order_cost(
            symbol="AAPL",
            buy_prices=buy_prices,
            quantity_per_order=5,
            currency="USD",
        )

        assert result["total_orders"] == 1
        assert result["total_quantity"] == 5
        assert result["total_cost"] == 500

    def test_empty_prices(self):
        """빈 가격 목록 테스트"""
        result = calculate_estimated_order_cost(
            symbol="BTC",
            buy_prices=[],
            quantity_per_order=0.001,
            currency="KRW",
        )

        assert result["total_orders"] == 0
        assert result["total_quantity"] == 0
        assert result["total_cost"] == 0

    def test_krw_integer_quantity(self):
        """KRW 통화에서 정수 수량 사용 테스트"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 50000},
        ]

        result = calculate_estimated_order_cost(
            symbol="005930",
            buy_prices=buy_prices,
            quantity_per_order=2.7,  # 소수점 입력
            currency="KRW",
        )

        # KRW는 정수로 변환됨
        assert result["buy_prices"][0]["quantity"] == 2

    def test_usd_decimal_quantity(self):
        """USD 통화에서 소수점 수량 유지 테스트"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 150},
        ]

        result = calculate_estimated_order_cost(
            symbol="AAPL",
            buy_prices=buy_prices,
            quantity_per_order=2.5,
            currency="USD",
        )

        # USD는 소수점 유지
        assert result["buy_prices"][0]["quantity"] == 2.5


class TestSymbolTradeSettingsService:
    """SymbolTradeSettingsService 테스트"""

    @pytest.fixture
    def mock_db(self):
        """Mock DB session"""
        db = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_create_settings(self, mock_db):
        """설정 생성 테스트"""
        service = SymbolTradeSettingsService(mock_db)

        # Mock the add and commit
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        result = await service.create(
            user_id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            buy_quantity_per_order=2,
            exchange_code=None,
            note="삼성전자 테스트",
        )

        # Verify add was called
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_by_symbol(self, mock_db):
        """심볼로 설정 조회 테스트"""
        service = SymbolTradeSettingsService(mock_db)

        # Mock settings
        mock_settings = SymbolTradeSettings(
            id=1,
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            buy_quantity_per_order=Decimal("2"),
            is_active=True,
        )

        # Mock execute result
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_settings
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await service.get_by_symbol("005930")

        assert result == mock_settings
        assert result.symbol == "005930"

    @pytest.mark.asyncio
    async def test_get_by_symbol_not_found(self, mock_db):
        """존재하지 않는 심볼 조회 테스트"""
        service = SymbolTradeSettingsService(mock_db)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await service.get_by_symbol("UNKNOWN")

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_settings(self, mock_db):
        """설정 삭제 테스트"""
        service = SymbolTradeSettingsService(mock_db)

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        result = await service.delete_settings("005930")

        assert result is True
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_settings_not_found(self, mock_db):
        """존재하지 않는 설정 삭제 테스트"""
        service = SymbolTradeSettingsService(mock_db)

        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        result = await service.delete_settings("UNKNOWN")

        assert result is False


class TestGetBuyQuantityFunctions:
    """수량 조회 함수 테스트"""

    @pytest.mark.asyncio
    async def test_get_buy_quantity_with_settings(self):
        """설정이 있을 때 수량 조회"""
        mock_db = AsyncMock()

        # Mock settings
        mock_settings = MagicMock()
        mock_settings.is_active = True
        mock_settings.buy_quantity_per_order = Decimal("5")

        with patch(
            "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
        ) as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.get_by_symbol.return_value = mock_settings
            MockService.return_value = mock_service_instance

            result = await get_buy_quantity_for_symbol(
                db=mock_db,
                symbol="005930",
                price=50000,
                fallback_amount=100000,
            )

            assert result == 5

    @pytest.mark.asyncio
    async def test_get_buy_quantity_without_settings(self):
        """설정이 없을 때 폴백 계산"""
        mock_db = AsyncMock()

        with patch(
            "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
        ) as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.get_by_symbol.return_value = None
            MockService.return_value = mock_service_instance

            result = await get_buy_quantity_for_symbol(
                db=mock_db,
                symbol="005930",
                price=50000,
                fallback_amount=100000,
            )

            # 100000 / 50000 = 2
            assert result == 2

    @pytest.mark.asyncio
    async def test_get_buy_quantity_inactive_settings(self):
        """비활성 설정일 때 폴백 계산"""
        mock_db = AsyncMock()

        mock_settings = MagicMock()
        mock_settings.is_active = False
        mock_settings.buy_quantity_per_order = Decimal("5")

        with patch(
            "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
        ) as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.get_by_symbol.return_value = mock_settings
            MockService.return_value = mock_service_instance

            result = await get_buy_quantity_for_symbol(
                db=mock_db,
                symbol="005930",
                price=50000,
                fallback_amount=100000,
            )

            # 비활성이므로 폴백 사용: 100000 / 50000 = 2
            assert result == 2

    @pytest.mark.asyncio
    async def test_get_buy_quantity_for_crypto_with_settings(self):
        """코인 수량 조회 (설정 있음)"""
        mock_db = AsyncMock()

        mock_settings = MagicMock()
        mock_settings.is_active = True
        # 코인의 경우 buy_quantity_per_order는 매수 금액(KRW)을 의미함
        # 50,000 KRW / 50,000,000 KRW/BTC = 0.001 BTC
        mock_settings.buy_quantity_per_order = Decimal("50000")

        with patch(
            "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
        ) as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.get_by_symbol.return_value = mock_settings
            MockService.return_value = mock_service_instance

            result = await get_buy_quantity_for_crypto(
                db=mock_db,
                symbol="BTC",
                price=50000000,
                fallback_amount=100000,
            )

            assert result == 0.001

    @pytest.mark.asyncio
    async def test_get_buy_quantity_for_crypto_without_settings(self):
        """코인 수량 조회 (설정 없음, 폴백)"""
        mock_db = AsyncMock()

        with patch(
            "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
        ) as MockService:
            mock_service_instance = AsyncMock()
            mock_service_instance.get_by_symbol.return_value = None
            MockService.return_value = mock_service_instance

            result = await get_buy_quantity_for_crypto(
                db=mock_db,
                symbol="BTC",
                price=50000000,
                fallback_amount=100000,
            )

            # 100000 / 50000000 = 0.002
            assert result == pytest.approx(0.002, rel=1e-6)


class TestSymbolTradeSettingsModel:
    """SymbolTradeSettings 모델 테스트"""

    def test_model_repr(self):
        """모델 __repr__ 테스트"""
        settings = SymbolTradeSettings(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            buy_quantity_per_order=Decimal("2"),
        )

        repr_str = repr(settings)
        assert "005930" in repr_str
        assert "equity_kr" in repr_str


class TestSymbolSettingsRouter:
    """Symbol Settings Router 테스트

    Note: 라우터 테스트는 인증 미들웨어 때문에 실제 API 호출이 어렵습니다.
    대신 서비스 레이어 테스트로 주요 로직을 검증합니다.
    """

    @pytest.fixture
    def client(self):
        """FastAPI TestClient with auth bypass"""
        from fastapi.testclient import TestClient
        from app.main import api

        # Auth middleware를 우회하기 위한 설정
        with patch("app.middleware.auth.AuthMiddleware.__call__") as mock_auth:
            async def bypass_auth(scope, receive, send):
                # 바로 다음 앱 호출
                await mock_auth.return_value.app(scope, receive, send)

            return TestClient(api)

    def test_calculate_estimated_order_cost_integration(self):
        """예상 비용 계산 통합 테스트 (서비스 레벨)"""
        # 실제 계산 로직 테스트
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 50000},
            {"price_name": "appropriate_buy_max", "price": 52000},
            {"price_name": "buy_hope_min", "price": 48000},
            {"price_name": "buy_hope_max", "price": 49000},
        ]

        result = calculate_estimated_order_cost(
            symbol="005930",
            buy_prices=buy_prices,
            quantity_per_order=3,
            currency="KRW",
        )

        # 4개 가격 × 3주 = 12주
        assert result["total_orders"] == 4
        assert result["total_quantity"] == 12
        # 총 비용: (50000 + 52000 + 48000 + 49000) × 3 = 597000
        expected_cost = (50000 + 52000 + 48000 + 49000) * 3
        assert result["total_cost"] == expected_cost

    def test_router_endpoint_exists(self):
        """라우터 엔드포인트 존재 확인"""
        from app.routers.symbol_settings import router

        # 라우터에 정의된 경로 확인
        routes = [route.path for route in router.routes]
        # 경로는 prefix 포함 형태로 저장됨
        assert any("/api/symbol-settings/" in r for r in routes)
        assert any("/api/symbol-settings/symbols/{symbol}" in r for r in routes)
        assert any("estimated-cost" in r for r in routes)
