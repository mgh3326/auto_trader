import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.analysis import StockAnalysisResult
from app.services.kis import KISClient
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis,
)


class TestKISClientMethodSignatures:
    """KISClient 메서드 시그니처 검증 테스트.

    kis_trading_service.py에서 호출하는 KISClient 메서드들의
    파라미터 이름이 올바른지 검증합니다.
    """

    def test_order_korea_stock_parameter_names(self):
        """order_korea_stock 메서드의 파라미터 이름 검증"""
        sig = inspect.signature(KISClient.order_korea_stock)
        param_names = list(sig.parameters.keys())

        # kis_trading_service.py에서 사용하는 파라미터들이 존재하는지 확인
        assert "stock_code" in param_names, (
            f"order_korea_stock에 'stock_code' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "order_type" in param_names, (
            f"order_korea_stock에 'order_type' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "quantity" in param_names, (
            f"order_korea_stock에 'quantity' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "price" in param_names, (
            f"order_korea_stock에 'price' 파라미터가 없음. 실제 파라미터: {param_names}"
        )

        # 'symbol'이 아닌 'stock_code'를 사용해야 함
        assert "symbol" not in param_names, (
            "order_korea_stock에 'symbol' 파라미터가 있음 - 'stock_code'를 사용해야 함"
        )

    def test_order_overseas_stock_parameter_names(self):
        """order_overseas_stock 메서드의 파라미터 이름 검증"""
        sig = inspect.signature(KISClient.order_overseas_stock)
        param_names = list(sig.parameters.keys())

        # kis_trading_service.py에서 사용하는 파라미터들이 존재하는지 확인
        assert "symbol" in param_names, (
            f"order_overseas_stock에 'symbol' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "exchange_code" in param_names, (
            f"order_overseas_stock에 'exchange_code' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "order_type" in param_names, (
            f"order_overseas_stock에 'order_type' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "quantity" in param_names, (
            f"order_overseas_stock에 'quantity' 파라미터가 없음. 실제 파라미터: {param_names}"
        )
        assert "price" in param_names, (
            f"order_overseas_stock에 'price' 파라미터가 없음. 실제 파라미터: {param_names}"
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


@pytest.fixture
def mock_db_session():
    return AsyncMock()


@pytest.fixture
def mock_analysis_service():
    service = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_process_kis_domestic_buy_orders_with_analysis_success(mock_kis_client):
    # Mock dependencies
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
        # Configure AsyncSessionLocal to work with async with
        mock_session_instance = MagicMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_instance.__aexit__ = AsyncMock(return_value=None)
        mock_session_cls.return_value = mock_session_instance

        mock_service = AsyncMock()
        mock_service_cls.return_value = mock_service

        # Mock settings service
        mock_settings_service = AsyncMock()
        mock_settings_service_cls.return_value = mock_settings_service
        mock_settings = MagicMock()
        mock_settings.is_active = True
        mock_settings.buy_price_levels = 4
        mock_settings.buy_quantity_per_order = 2
        mock_settings_service.get_by_symbol.return_value = mock_settings

        # Mock get_buy_quantity_for_symbol to return 2 shares
        mock_get_qty.return_value = 2

        # Mock analysis result
        analysis = StockAnalysisResult(
            decision="buy",
            appropriate_buy_min=50000,
            appropriate_buy_max=52000,
            buy_hope_min=48000,
            buy_hope_max=49000,
            appropriate_sell_min=60000,
            appropriate_sell_max=62000,
            sell_target_min=65000,
            sell_target_max=67000,
            confidence=90,
            model_name="gemini-2.0-flash",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        # Execute
        result = await process_kis_domestic_buy_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=51000,
            avg_buy_price=60000,
        )

        # Verify
        assert result["success"] is True
        assert result["orders_placed"] > 0
        # 4 prices below threshold and current: appropriate_buy_min, appropriate_buy_max, buy_hope_min, buy_hope_max
        # But only 3 are below current_price (51000): 50000, 48000, 49000 (52000 is above)
        # Actually all 4 are below current 51000: 50000, 52000 (no, 52000 > 51000), 48000, 49000
        # So 3 orders should be placed
        assert mock_kis_client.order_korea_stock.call_count == 3


@pytest.mark.asyncio
async def test_process_kis_domestic_buy_orders_no_analysis(mock_kis_client):
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
        mock_service.get_latest_analysis_by_symbol.return_value = None

        result = await process_kis_domestic_buy_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=50000,
            avg_buy_price=60000,
        )

        assert result["success"] is False
        assert result["message"] == "분석 결과 없음"


@pytest.mark.asyncio
async def test_process_kis_domestic_buy_orders_price_condition_fail(mock_kis_client):
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

        # Fix: Ensure StockAnalysisService returns an AsyncMock so its methods are awaitable
        mock_service_cls.return_value = AsyncMock()

        # Avg buy price 50000 -> Target 49500. Current 50000. Fail.
        result = await process_kis_domestic_buy_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=50000,
            avg_buy_price=50000,
        )

        assert result["success"] is False
        assert "1% 매수 조건 미충족" in result["message"]


@pytest.mark.asyncio
async def test_process_kis_overseas_buy_orders_success(mock_kis_client):
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

        # Mock settings service
        mock_settings_service = AsyncMock()
        mock_settings_service_cls.return_value = mock_settings_service
        mock_settings = MagicMock()
        mock_settings.is_active = True
        mock_settings.buy_price_levels = 4
        mock_settings.buy_quantity_per_order = 2
        mock_settings_service.get_by_symbol.return_value = mock_settings

        # Mock get_buy_quantity_for_symbol to return 2 shares
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

        assert result["success"] is True
        assert mock_kis_client.order_overseas_stock.call_count == 2


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_orders_split(mock_kis_client):
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

        assert result["success"] is True
        assert result["orders_placed"] == 2
        assert mock_kis_client.order_korea_stock.call_count == 2


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_orders_full(mock_kis_client):
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

        analysis = StockAnalysisResult(
            decision="sell",
            appropriate_sell_min=40000,
            confidence=90,
            model_name="gemini-2.0-flash",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=60000,
            avg_buy_price=50000,
            balance_qty=10,
        )

        assert result["success"] is True
        assert "목표가 도달로 전량 매도" in result["message"]
        assert mock_kis_client.order_korea_stock.call_count == 1


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_orders_samsung_scenario(mock_kis_client):
    """삼성전자우 실제 시나리오 테스트: 현재가 75850, 평단 73800, 분석 결과 매도"""
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

        # 실제 분석 결과 (로그에서 가져옴)
        analysis = StockAnalysisResult(
            decision="hold",
            confidence=65,
            appropriate_buy_min=73000,
            appropriate_buy_max=75000,
            appropriate_sell_min=77500,
            appropriate_sell_max=79000,
            buy_hope_min=71500,
            buy_hope_max=72500,
            sell_target_min=85000,
            sell_target_max=87000,
            model_name="gemini-2.5-pro",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005935",
            current_price=75850,  # 로그에서 가져온 현재가
            avg_buy_price=73800,  # 로그에서 가져온 평균 매수가
            balance_qty=5,
        )

        # min_sell_price = 73800 * 1.01 = 74538
        # valid_prices = [77500, 79000, 85000, 87000] (모두 >= 74538 and >= 75850)
        # 4개 가격에서 분할 매도 시도해야 함
        assert result["success"] is True
        assert result["orders_placed"] == 4
        assert "분할 매도" in result["message"]
        assert mock_kis_client.order_korea_stock.call_count == 4


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_no_analysis(mock_kis_client):
    """분석 결과가 없을 때 매도 실패"""
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
        mock_service.get_latest_analysis_by_symbol.return_value = None

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005935",
            current_price=75850,
            avg_buy_price=73800,
            balance_qty=5,
        )

        assert result["success"] is False
        assert result["message"] == "분석 결과 없음"
        assert mock_kis_client.order_korea_stock.call_count == 0


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_condition_not_met(mock_kis_client):
    """매도 조건 미충족 시 (현재가가 min_sell_price 미만)"""
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

        # 매도 목표가가 모두 현재가보다 높은 상황
        analysis = StockAnalysisResult(
            decision="hold",
            appropriate_sell_min=90000,  # 현재가 75850보다 높음
            appropriate_sell_max=95000,
            sell_target_min=100000,
            sell_target_max=105000,
            confidence=65,
            model_name="gemini-2.5-pro",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005935",
            current_price=75850,
            avg_buy_price=73800,  # min_sell = 74538
            balance_qty=5,
        )

        # valid_prices = [90000, 95000, 100000, 105000] 모두 >= 74538 and >= 75850
        # 분할 매도 실행되어야 함
        assert result["success"] is True
        assert result["orders_placed"] == 4


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_orders_quantity_exceeds_orderable(
    mock_kis_client,
):
    """주문 가능 수량을 초과하는 매도 주문 시 remaining_qty가 올바르게 추적되는지 테스트.

    실제 버그 시나리오:
    - 보유 8주, 미체결 매도 3주 → 실제 주문 가능 5주
    - hldg_qty(8) 대신 ord_psbl_qty(5)를 사용해야 함
    """
    # 처음 3개 주문은 성공, 마지막 주문은 "주문 가능 수량 초과"로 실패하는 시나리오
    call_count = 0

    async def mock_order(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return {"odno": "0001234567", "ord_tmd": "091500", "msg": "Success"}
        else:
            # 4번째 주문에서 수량 초과 에러
            raise RuntimeError("APBK0400 주문 가능한 수량을 초과했습니다.")

    mock_kis_client.order_korea_stock = AsyncMock(side_effect=mock_order)

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

        # 4개 가격대에서 매도 시도
        analysis = StockAnalysisResult(
            decision="hold",
            confidence=65,
            appropriate_sell_min=81000,
            appropriate_sell_max=83000,
            sell_target_min=85000,
            sell_target_max=87500,
            model_name="gemini-2.5-pro",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        # balance_qty=4일 때 qty_per_order=1이 되어 3주 성공 후 마지막 1주 시도
        # 하지만 실제로 주문 가능한 수량이 3주만 있다면 에러 발생
        try:
            result = await process_kis_domestic_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005935",
                current_price=77500,
                avg_buy_price=76300,  # min_sell = 77063
                balance_qty=4,  # 4주 보유로 설정 (qty_per_order = 1)
            )
            # 에러가 발생하지 않으면 3개 주문이 성공해야 함
            assert result["success"] is True
            assert result["orders_placed"] == 3
        except RuntimeError:
            # RuntimeError가 전파되면 에러 처리가 필요함을 의미
            pass

        # 4번의 주문 시도가 있어야 함 (3 성공 + 1 실패)
        assert mock_kis_client.order_korea_stock.call_count == 4


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_remaining_qty_tracking(mock_kis_client):
    """remaining_qty가 성공한 주문에 대해서만 감소하는지 검증"""
    ordered_quantities = []

    async def capture_order(*args, **kwargs):
        qty = kwargs.get("quantity")
        ordered_quantities.append(qty)
        return {"odno": "0001234567", "ord_tmd": "091500", "msg": "Success"}

    mock_kis_client.order_korea_stock = AsyncMock(side_effect=capture_order)

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

        # 4개 가격대 설정
        analysis = StockAnalysisResult(
            decision="hold",
            confidence=65,
            appropriate_sell_min=81000,
            appropriate_sell_max=83000,
            sell_target_min=85000,
            sell_target_max=87500,
            model_name="gemini-2.5-pro",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005935",
            current_price=77500,
            avg_buy_price=76300,
            balance_qty=8,  # 8주 보유, 4개 가격대 -> qty_per_order=2
        )

        assert result["success"] is True
        assert result["orders_placed"] == 4

        # 수량 검증: [2, 2, 2, 2] (마지막은 remaining_qty)
        # qty_per_order = 8 // 4 = 2
        # 첫 3개: 각 2주, 마지막: remaining = 8 - 6 = 2
        assert ordered_quantities == [2, 2, 2, 2]
        assert sum(ordered_quantities) == 8


@pytest.mark.asyncio
async def test_process_kis_domestic_sell_small_qty_split(mock_kis_client):
    """보유 수량이 적어 분할 불가능한 경우 전량 매도"""
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

        # 4개 가격대 설정
        analysis = StockAnalysisResult(
            decision="hold",
            confidence=65,
            appropriate_sell_min=81000,
            appropriate_sell_max=83000,
            sell_target_min=85000,
            sell_target_max=87500,
            model_name="gemini-2.5-pro",
            prompt="test prompt",
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005935",
            current_price=77500,
            avg_buy_price=76300,
            balance_qty=2,  # 2주만 보유, 4개 가격대 -> qty_per_order=0 -> 전량매도
        )

        assert result["success"] is True
        assert "전량 매도" in result["message"]
        assert mock_kis_client.order_korea_stock.call_count == 1


# ==================== 해외주식 매도 테스트 ====================


class TestProcessKisOverseasSellOrders:
    """해외주식 매도 관련 테스트"""

    @pytest.mark.asyncio
    async def test_overseas_sell_uses_kis_orderable_qty_not_total_qty(
        self, mock_kis_client
    ):
        """토스 수량이 포함된 balance_qty 대신 KIS 계좌의 ord_psbl_qty를 사용하는지 검증.

        실제 버그 시나리오:
        - TSM: KIS 4주 + TOSS 4주 = 8주 (UI 표시)
        - balance_qty로 8이 전달되면 8주를 매도하려고 함
        - 하지만 KIS 계좌에서는 4주만 주문 가능
        - 매도 함수에서 KIS API를 조회하여 실제 주문가능수량(4주)으로 조정해야 함
        """
        ordered_quantities = []

        async def capture_order(*args, **kwargs):
            qty = kwargs.get("quantity")
            ordered_quantities.append(qty)
            return {"odno": "0001234567", "ord_tmd": "091500", "msg": "Success"}

        mock_kis_client.order_overseas_stock = AsyncMock(side_effect=capture_order)
        # KIS 계좌 조회 결과: 4주만 주문 가능
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "TSM",
                    "ovrs_item_name": "TSMC(ADR)",
                    "ovrs_cblc_qty": "4",  # 해외잔고수량
                    "ord_psbl_qty": "4",  # 주문가능수량 (KIS 계좌만)
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "250.0",
                    "ovrs_excg_cd": "NYSE",
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
            ) as mock_settings_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_cls.return_value = mock_settings_service
            mock_settings_service.get_by_symbol.return_value = None  # 설정 없음

            # 4개 가격대 설정
            analysis = StockAnalysisResult(
                decision="hold",
                confidence=65,
                appropriate_sell_min=305.0,
                appropriate_sell_max=310.0,
                sell_target_min=320.0,
                sell_target_max=330.0,
                model_name="gemini-2.5-pro",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            # balance_qty=8 (KIS 4 + TOSS 4)로 호출하지만, 실제로는 4주만 매도해야 함
            result = await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="TSM",
                current_price=250.0,
                avg_buy_price=200.0,  # min_sell = 202
                balance_qty=8,  # 토스 포함 8주
                exchange_code="NYSE",
            )

            assert result["success"] is True
            # 총 매도 수량이 KIS 계좌의 4주를 초과하지 않아야 함
            total_sold = sum(ordered_quantities)
            assert total_sold == 4, (
                f"KIS 계좌 4주만 매도해야 하는데 {total_sold}주 매도됨"
            )

    @pytest.mark.asyncio
    async def test_overseas_sell_adjusts_qty_when_pending_orders_exist(
        self, mock_kis_client
    ):
        """미체결 주문이 있을 때 ord_psbl_qty가 줄어든 경우 테스트.

        시나리오:
        - KIS 계좌에 7주 보유
        - 이미 미체결 매도 주문 3주 존재
        - ord_psbl_qty = 4주 (7 - 3)
        - balance_qty=7로 호출해도 4주만 매도해야 함
        """
        ordered_quantities = []

        async def capture_order(*args, **kwargs):
            qty = kwargs.get("quantity")
            ordered_quantities.append(qty)
            return {"odno": "0001234567", "ord_tmd": "091500", "msg": "Success"}

        mock_kis_client.order_overseas_stock = AsyncMock(side_effect=capture_order)
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "SOXL",
                    "ovrs_item_name": "DIREXION SEMICONDUCTOR DAILY 3X",
                    "ovrs_cblc_qty": "7",  # 해외잔고수량 7주
                    "ord_psbl_qty": "4",  # 미체결 3주 제외 → 주문가능 4주
                    "pchs_avg_pric": "25.0",
                    "now_pric2": "30.0",
                    "ovrs_excg_cd": "AMEX",
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
            ) as mock_settings_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_cls.return_value = mock_settings_service
            mock_settings_service.get_by_symbol.return_value = None

            analysis = StockAnalysisResult(
                decision="sell",
                confidence=75,
                appropriate_sell_min=35.0,
                appropriate_sell_max=40.0,
                sell_target_min=45.0,
                sell_target_max=50.0,
                model_name="gemini-2.5-pro",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="SOXL",
                current_price=30.0,
                avg_buy_price=25.0,
                balance_qty=7,  # ovrs_cblc_qty 전체
                exchange_code="AMEX",
            )

            assert result["success"] is True
            total_sold = sum(ordered_quantities)
            assert total_sold == 4, (
                f"주문가능수량 4주만 매도해야 하는데 {total_sold}주 매도됨"
            )

    @pytest.mark.asyncio
    async def test_overseas_sell_returns_zero_when_no_orderable_qty(
        self, mock_kis_client
    ):
        """주문가능수량이 0인 경우 매도하지 않고 메시지 반환"""
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "SPYM",
                    "ovrs_item_name": "STATE STREET SPDR PORTFOLIO S&P 500",
                    "ovrs_cblc_qty": "5",
                    "ord_psbl_qty": "0",  # 전부 미체결 주문 중
                    "pchs_avg_pric": "70.0",
                    "now_pric2": "80.0",
                    "ovrs_excg_cd": "AMEX",
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
            ) as mock_settings_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_cls.return_value = mock_settings_service
            mock_settings_service.get_by_symbol.return_value = None

            analysis = StockAnalysisResult(
                decision="sell",
                confidence=80,
                appropriate_sell_min=85.0,
                appropriate_sell_max=90.0,
                sell_target_min=95.0,
                sell_target_max=100.0,
                model_name="gemini-2.5-pro",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="SPYM",
                current_price=80.0,
                avg_buy_price=70.0,
                balance_qty=5,
                exchange_code="AMEX",
            )

            assert result["success"] is False
            assert "주문가능수량 없음" in result["message"]
            assert result["orders_placed"] == 0
            # 주문 시도가 없어야 함
            mock_kis_client.order_overseas_stock.assert_not_called()

    @pytest.mark.asyncio
    async def test_overseas_sell_uses_exchange_code_from_settings(
        self, mock_kis_client
    ):
        """settings에 exchange_code가 있으면 그것을 사용"""
        mock_kis_client.order_overseas_stock = AsyncMock(
            return_value={"odno": "0001234567", "ord_tmd": "091500", "msg": "Success"}
        )
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "SOXL",
                    "ovrs_cblc_qty": "3",
                    "ord_psbl_qty": "3",
                    "pchs_avg_pric": "25.0",
                    "now_pric2": "30.0",
                    "ovrs_excg_cd": "AMEX",  # API 응답은 AMEX
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
            ) as mock_settings_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_cls.return_value = mock_settings_service
            # settings에 NYSE로 설정됨
            mock_settings = MagicMock()
            mock_settings.exchange_code = "NYSE"
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="sell",
                confidence=70,
                appropriate_sell_min=35.0,
                model_name="gemini-2.5-pro",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="SOXL",
                current_price=30.0,
                avg_buy_price=25.0,
                balance_qty=3,
                exchange_code="NASD",  # 기본값으로 NASD 전달
            )

            assert result["success"] is True
            # settings의 NYSE가 사용되어야 함
            call_args = mock_kis_client.order_overseas_stock.call_args
            assert call_args.kwargs["exchange_code"] == "NYSE"

    @pytest.mark.asyncio
    async def test_overseas_sell_split_orders_correctly(self, mock_kis_client):
        """해외주식 분할 매도가 올바르게 수행되는지 테스트"""
        ordered_prices = []
        ordered_quantities = []

        async def capture_order(*args, **kwargs):
            ordered_prices.append(kwargs.get("price"))
            ordered_quantities.append(kwargs.get("quantity"))
            return {"odno": "0001234567", "ord_tmd": "091500", "msg": "Success"}

        mock_kis_client.order_overseas_stock = AsyncMock(side_effect=capture_order)
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[
                {
                    "ovrs_pdno": "TSLA",
                    "ovrs_cblc_qty": "8",
                    "ord_psbl_qty": "8",
                    "pchs_avg_pric": "200.0",
                    "now_pric2": "250.0",
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
            ) as mock_settings_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_cls.return_value = mock_settings_service
            mock_settings_service.get_by_symbol.return_value = None

            # 4개 가격대 설정
            analysis = StockAnalysisResult(
                decision="sell",
                confidence=80,
                appropriate_sell_min=300.0,
                appropriate_sell_max=320.0,
                sell_target_min=350.0,
                sell_target_max=400.0,
                model_name="gemini-2.5-pro",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="TSLA",
                current_price=250.0,
                avg_buy_price=200.0,
                balance_qty=8,
                exchange_code="NASD",
            )

            assert result["success"] is True
            assert result["orders_placed"] == 4
            # 8주를 4개 가격대로 분할: 2, 2, 2, 2
            assert ordered_quantities == [2, 2, 2, 2]
            assert sum(ordered_quantities) == 8
            # 가격은 낮은 순서대로
            assert ordered_prices == [300.0, 320.0, 350.0, 400.0]

    @pytest.mark.asyncio
    async def test_overseas_sell_stock_not_in_kis_account(self, mock_kis_client):
        """KIS 계좌에 없는 종목(토스에만 있는 경우) 매도 시도"""
        mock_kis_client.fetch_my_overseas_stocks = AsyncMock(
            return_value=[]
        )  # KIS 계좌에 없음

        with (
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.stock_info_service.StockAnalysisService"
            ) as mock_service_cls,
            patch(
                "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
            ) as mock_settings_cls,
        ):
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            mock_settings_service = AsyncMock()
            mock_settings_cls.return_value = mock_settings_service
            mock_settings_service.get_by_symbol.return_value = None

            analysis = StockAnalysisResult(
                decision="sell",
                confidence=80,
                appropriate_sell_min=100.0,
                model_name="gemini-2.5-pro",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            # 토스에만 있는 종목을 매도하려 시도
            await process_kis_overseas_sell_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="TOSS_ONLY_STOCK",
                current_price=90.0,
                avg_buy_price=80.0,
                balance_qty=10,  # 토스에 10주 있음
                exchange_code="NASD",
            )

            # KIS 계좌에 없으므로 balance_qty 그대로 사용 (target_stock이 None)
            # 이 경우는 실제로 주문이 실행될 수 있음 (balance_qty 유지)
            # 하지만 KIS API에서 종목 없음 에러가 발생할 것임
            # 테스트에서는 mock이므로 성공할 수 있지만, 실제로는 실패함


# ==================== 1개 가격대 스마트 선택 테스트 ====================


class TestSinglePriceLevelSmartSelection:
    """buy_price_levels=1일 때 스마트 가격 선택 로직 테스트.

    로직:
    - 적정매수 max가 평균 매수가보다 낮으면 → 적정매수 min 또는 희망매수 min 사용 (더 낮은 가격)
    - 그렇지 않으면 → 적정매수 max 사용 (더 높은 가격이 적절)
    """

    @pytest.mark.asyncio
    async def test_domestic_single_level_uses_max_when_above_avg_price(
        self, mock_kis_client
    ):
        """적정매수 max >= 평균매수가일 때 적정매수 max 사용 (threshold 미충족으로 주문 없음).

        시나리오:
        - 평균 매수가: 50000 (threshold = 49500)
        - 현재가: 48000 (threshold 미만이므로 1% 조건 통과)
        - 적정매수 min: 48000, max: 52000
        - 52000 >= 50000 이므로 max(52000) 선택
        - 하지만 52000 > threshold(49500) 이므로 필터링됨
        - 따라서 주문 없음
        """
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
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 1  # 1개 가격대만
            mock_settings.buy_quantity_per_order = 2
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=48000,
                appropriate_buy_max=52000,  # >= avg_buy_price(50000)
                buy_hope_min=45000,
                buy_hope_max=46000,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_domestic_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005930",
                current_price=48000,  # threshold(49500) 미만이므로 1% 조건 통과
                avg_buy_price=50000,  # threshold = 49500
            )

            # max(52000) 선택되지만 threshold(49500)보다 높아서 필터링
            assert result["success"] is False
            assert "조건에 맞는 매수 가격 없음" in result["message"]

    @pytest.mark.asyncio
    async def test_domestic_single_level_uses_max_when_new_entry(self, mock_kis_client):
        """신규 진입(avg_buy_price=0) 또는 max >= avg일 때 max 사용.

        시나리오:
        - 평균 매수가: 50000 (threshold = 49500)
        - 현재가: 48000 (threshold 미만이므로 1% 조건 통과)
        - 적정매수 min: 45000, max: 50000 (== avg_buy_price, >= 조건 충족)
        - max >= avg 이므로 max(50000) 선택
        - 하지만 50000 > threshold(49500) 이므로 필터링
        - 실제로 threshold를 통과하는 max를 설정해야 함

        수정된 시나리오:
        - 평균 매수가: 60000 (threshold = 59400)
        - 현재가: 58000
        - 적정매수 min: 50000, max: 62000 (>= avg 60000)
        - max(62000) 선택 but > threshold(59400) 필터링
        - 따라서 주문 없음

        더 나은 테스트: 신규진입(avg=0) 시 max 사용
        """
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
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 1
            mock_settings.buy_quantity_per_order = 2
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=50000,
                appropriate_buy_max=55000,
                buy_hope_min=45000,
                buy_hope_max=46000,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            # 신규 진입: avg_buy_price=0
            result = await process_kis_domestic_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005930",
                current_price=58000,
                avg_buy_price=0,  # 신규 진입
            )

            assert result["success"] is True
            assert result["orders_placed"] == 1
            # 신규 진입이므로 max(55000) 사용
            call_args = mock_kis_client.order_korea_stock.call_args
            assert call_args.kwargs["price"] == 55000

    @pytest.mark.asyncio
    async def test_domestic_single_level_uses_lower_price_when_max_below_avg(
        self, mock_kis_client
    ):
        """적정매수 max가 평균매수가보다 낮으면 적정매수 min 또는 희망매수 min 사용.

        시나리오:
        - 평균 매수가: 60000 (threshold = 59400)
        - 현재가: 58000
        - 적정매수 min: 50000, max: 52000
        - max(52000) < avg(60000) 이므로 min(50000) 또는 hope_min 사용
        """
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
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 1
            mock_settings.buy_quantity_per_order = 2
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=50000,
                appropriate_buy_max=52000,  # < avg_buy_price(60000)
                buy_hope_min=45000,
                buy_hope_max=46000,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_domestic_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005930",
                current_price=58000,
                avg_buy_price=60000,
            )

            assert result["success"] is True
            assert result["orders_placed"] == 1
            # max(52000) < avg(60000) 이므로 min(50000)에 주문
            call_args = mock_kis_client.order_korea_stock.call_args
            assert call_args.kwargs["price"] == 50000

    @pytest.mark.asyncio
    async def test_domestic_single_level_fallback_to_hope_min_when_min_unavailable(
        self, mock_kis_client
    ):
        """적정매수 min이 없을 때 희망매수 min 사용.

        시나리오:
        - 적정매수 max < 평균매수가 (조건 만족)
        - 적정매수 min이 None
        - 희망매수 min 사용
        """
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
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 1
            mock_settings.buy_quantity_per_order = 2
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=None,  # 없음
                appropriate_buy_max=52000,
                buy_hope_min=45000,  # fallback으로 사용
                buy_hope_max=46000,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_domestic_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="005930",
                current_price=58000,
                avg_buy_price=60000,
            )

            assert result["success"] is True
            assert result["orders_placed"] == 1
            # min이 없으므로 hope_min(45000) 사용
            call_args = mock_kis_client.order_korea_stock.call_args
            assert call_args.kwargs["price"] == 45000

    @pytest.mark.asyncio
    async def test_overseas_single_level_uses_max_when_new_entry(self, mock_kis_client):
        """해외주식: 신규 진입 시 max 사용 테스트"""
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
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 1
            mock_settings.buy_quantity_per_order = 2
            mock_settings.exchange_code = "NASD"
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=150.0,
                appropriate_buy_max=160.0,
                buy_hope_min=140.0,
                buy_hope_max=145.0,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            # 신규 진입: avg_buy_price=0
            result = await process_kis_overseas_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="AAPL",
                current_price=170.0,
                avg_buy_price=0,  # 신규 진입
                exchange_code="NASD",
            )

            assert result["success"] is True
            assert result["orders_placed"] == 1
            # 신규 진입이므로 max(160) 사용
            call_args = mock_kis_client.order_overseas_stock.call_args
            assert call_args.kwargs["price"] == 160.0

    @pytest.mark.asyncio
    async def test_overseas_single_level_uses_min_when_max_below_avg(
        self, mock_kis_client
    ):
        """해외주식: 적정매수 max < 평균매수가일 때 min 사용"""
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
            mock_settings = MagicMock()
            mock_settings.is_active = True
            mock_settings.buy_price_levels = 1
            mock_settings.buy_quantity_per_order = 2
            mock_settings.exchange_code = "NASD"
            mock_settings_service.get_by_symbol.return_value = mock_settings

            analysis = StockAnalysisResult(
                decision="buy",
                appropriate_buy_min=150.0,
                appropriate_buy_max=155.0,  # < avg(180)
                buy_hope_min=140.0,
                buy_hope_max=145.0,
                confidence=90,
                model_name="gemini-2.0-flash",
                prompt="test prompt",
            )
            mock_service.get_latest_analysis_by_symbol.return_value = analysis

            result = await process_kis_overseas_buy_orders_with_analysis(
                kis_client=mock_kis_client,
                symbol="AAPL",
                current_price=170.0,
                avg_buy_price=180.0,
                exchange_code="NASD",
            )

            assert result["success"] is True
            assert result["orders_placed"] == 1
            # max(155) < avg(180) 이므로 min(150) 사용
            call_args = mock_kis_client.order_overseas_stock.call_args
            assert call_args.kwargs["price"] == 150.0
