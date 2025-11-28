import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis,
)
from app.models.analysis import StockAnalysisResult

@pytest.fixture
def mock_kis_client():
    client = AsyncMock()
    client.order_korea_stock.return_value = {'rt_cd': '0', 'msg1': 'Success'}
    client.order_overseas_stock.return_value = {'rt_cd': '0', 'msg1': 'Success'}
    client.get_balance.return_value = {'output2': [{'dnca_tot_amt': '1000000'}]}
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
    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls, \
         patch('app.services.symbol_trade_settings_service.get_buy_quantity_for_symbol') as mock_get_qty:

        # Configure AsyncSessionLocal to work with async with
        mock_session_instance = MagicMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_instance.__aexit__ = AsyncMock(return_value=None)
        mock_session_cls.return_value = mock_session_instance

        mock_service = AsyncMock()
        mock_service_cls.return_value = mock_service

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
            prompt="test prompt"
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        # Execute
        result = await process_kis_domestic_buy_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=51000,
            avg_buy_price=60000
        )

        # Verify
        assert result['success'] is True
        assert result['orders_placed'] > 0
        # 4 prices below threshold and current: appropriate_buy_min, appropriate_buy_max, buy_hope_min, buy_hope_max
        # But only 3 are below current_price (51000): 50000, 48000, 49000 (52000 is above)
        # Actually all 4 are below current 51000: 50000, 52000 (no, 52000 > 51000), 48000, 49000
        # So 3 orders should be placed
        assert mock_kis_client.order_korea_stock.call_count == 3

@pytest.mark.asyncio
async def test_process_kis_domestic_buy_orders_no_analysis(mock_kis_client):
    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls:
        
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
            avg_buy_price=60000
        )

        assert result['success'] is False
        assert result['message'] == "분석 결과 없음"

@pytest.mark.asyncio
async def test_process_kis_domestic_buy_orders_price_condition_fail(mock_kis_client):
    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls:
        
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
            avg_buy_price=50000
        )

        assert result['success'] is False
        assert "1% 매수 조건 미충족" in result['message']

@pytest.mark.asyncio
async def test_process_kis_overseas_buy_orders_success(mock_kis_client):
    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls, \
         patch('app.services.symbol_trade_settings_service.get_buy_quantity_for_symbol') as mock_get_qty:

        mock_session_instance = MagicMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_instance.__aexit__ = AsyncMock(return_value=None)
        mock_session_cls.return_value = mock_session_instance

        mock_service = AsyncMock()
        mock_service_cls.return_value = mock_service

        # Mock get_buy_quantity_for_symbol to return 2 shares
        mock_get_qty.return_value = 2

        analysis = StockAnalysisResult(
            decision="buy",
            appropriate_buy_min=50,
            appropriate_buy_max=55,
            confidence=90,
            model_name="gemini-2.0-flash",
            prompt="test prompt"
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_overseas_buy_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="AAPL",
            current_price=60,
            avg_buy_price=80,
            exchange_code="NASD"
        )

        assert result['success'] is True
        assert mock_kis_client.order_overseas_stock.call_count == 2

@pytest.mark.asyncio
async def test_process_kis_domestic_sell_orders_split(mock_kis_client):
    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls:
        
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
            prompt="test prompt"
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=55000,
            avg_buy_price=50000,
            balance_qty=10
        )

        assert result['success'] is True
        assert result['orders_placed'] == 2
        assert mock_kis_client.order_korea_stock.call_count == 2

@pytest.mark.asyncio
async def test_process_kis_domestic_sell_orders_full(mock_kis_client):
    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls:
        
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
            prompt="test prompt"
        )
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        result = await process_kis_domestic_sell_orders_with_analysis(
            kis_client=mock_kis_client,
            symbol="005930",
            current_price=60000,
            avg_buy_price=50000,
            balance_qty=10
        )

        assert result['success'] is True
        assert "목표가 도달로 전량 매도" in result['message']
        assert mock_kis_client.order_korea_stock.call_count == 1
