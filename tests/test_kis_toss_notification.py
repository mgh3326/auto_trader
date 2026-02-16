"""
Tests for Toss notification integration in KIS automation tasks.

종목별 자동 실행 시 토스 수동 잔고가 있을 때 텔레그램 메시지 발송 테스트
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_toss_service():
    """Create a mock TossNotificationService."""
    service = MagicMock()
    service.process_analysis_result = AsyncMock(return_value=True)
    return service


@pytest.fixture
def mock_stock_analysis_result():
    """Create a mock StockAnalysisResult."""
    from app.models.analysis import StockAnalysisResult

    return StockAnalysisResult(
        decision="buy",
        confidence=75,
        appropriate_buy_min=68000,
        appropriate_buy_max=70000,
        appropriate_sell_min=75000,
        appropriate_sell_max=77000,
        buy_hope_min=65000,
        buy_hope_max=67000,
        sell_target_min=80000,
        sell_target_max=82000,
        model_name="gemini-2.5-pro",
        prompt="test prompt",
        reasons=["기술적 분석 상 매수 시점", "거래량 증가", "긍정적 재무제표"],
    )


class TestDomesticStockTossNotification:
    """국내 주식 자동 실행 시 토스 알림 테스트"""

    @pytest.mark.asyncio
    async def test_analyze_domestic_stock_sends_toss_notification_on_buy(
        self, mock_db, mock_toss_service, mock_stock_analysis_result
    ):
        """
        국내 주식 분석 시 buy 결정이고 토스 잔고가 있으면 알림을 보내야 함
        """
        from app.jobs import kis_trading as kis_tasks
        from app.models.manual_holdings import MarketType

        # Mock analyzer result
        mock_stock_analysis_result.decision = "buy"

        with (
            patch("app.jobs.kis_trading.KISClient") as MockKIS,
            patch("app.jobs.kis_trading.KISAnalyzer") as MockAnalyzer,
            patch("app.jobs.kis_trading.get_trade_notifier") as mock_get_notifier,
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.toss_notification_service.send_toss_notification_if_needed"
            ) as mock_send_toss,
        ):
            # Setup KIS mock
            mock_kis = MockKIS.return_value
            mock_kis.fetch_fundamental_info = AsyncMock(
                return_value={"종목명": "삼성전자", "현재가": 70000}
            )

            # Setup analyzer mock
            mock_analyzer_instance = MockAnalyzer.return_value
            mock_analyzer_instance.analyze_stock_json = AsyncMock(
                return_value=(mock_stock_analysis_result, "gemini-2.5-pro")
            )
            mock_analyzer_instance.close = AsyncMock(return_value=None)

            # Setup notifier mock
            mock_notifier = MagicMock()
            mock_notifier.notify_analysis_complete = AsyncMock(return_value=True)
            mock_get_notifier.return_value = mock_notifier

            # Setup DB mock
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            # Setup Toss notification mock
            mock_send_toss.return_value = True

            # Execute
            result = await kis_tasks._analyze_domestic_stock_async("005930")

            # Assertions
            assert result["status"] == "completed"
            assert result["symbol"] == "005930"

            # Verify Toss notification was called
            mock_send_toss.assert_called_once()

            call_kwargs = mock_send_toss.call_args.kwargs
            assert call_kwargs["ticker"] == "005930"
            assert call_kwargs["name"] == "삼성전자"
            assert call_kwargs["market_type"] == MarketType.KR
            assert call_kwargs["decision"] == "buy"
            assert call_kwargs["current_price"] == 70000.0
            assert call_kwargs["recommended_buy_price"] == 68000  # appropriate_buy_min

    @pytest.mark.asyncio
    async def test_analyze_domestic_stock_sends_toss_notification_on_sell(
        self, mock_db, mock_stock_analysis_result
    ):
        """
        국내 주식 분석 시 sell 결정이고 토스 잔고가 있으면 알림을 보내야 함
        """
        from app.jobs import kis_trading as kis_tasks

        # Mock analyzer result
        mock_stock_analysis_result.decision = "sell"

        with (
            patch("app.jobs.kis_trading.KISClient") as MockKIS,
            patch("app.jobs.kis_trading.KISAnalyzer") as MockAnalyzer,
            patch("app.jobs.kis_trading.get_trade_notifier") as mock_get_notifier,
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.toss_notification_service.send_toss_notification_if_needed"
            ) as mock_send_toss,
        ):
            # Setup KIS mock
            mock_kis = MockKIS.return_value
            mock_kis.fetch_fundamental_info = AsyncMock(
                return_value={"종목명": "카카오", "현재가": 50000}
            )

            # Setup analyzer mock
            mock_analyzer_instance = MockAnalyzer.return_value
            mock_analyzer_instance.analyze_stock_json = AsyncMock(
                return_value=(mock_stock_analysis_result, "gemini-2.5-pro")
            )
            mock_analyzer_instance.close = AsyncMock(return_value=None)

            # Setup notifier mock
            mock_notifier = MagicMock()
            mock_notifier.notify_analysis_complete = AsyncMock(return_value=True)
            mock_get_notifier.return_value = mock_notifier

            # Setup DB mock
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            # Setup Toss notification mock
            mock_send_toss.return_value = True

            # Execute
            result = await kis_tasks._analyze_domestic_stock_async("035720")

            # Assertions
            assert result["status"] == "completed"

            # Verify Toss notification was called with sell decision
            mock_send_toss.assert_called_once()

            call_kwargs = mock_send_toss.call_args.kwargs
            assert call_kwargs["decision"] == "sell"
            assert (
                call_kwargs["recommended_sell_price"] == 75000
            )  # appropriate_sell_min

    @pytest.mark.asyncio
    async def test_analyze_domestic_stock_no_toss_notification_on_hold(
        self, mock_db, mock_stock_analysis_result
    ):
        """
        국내 주식 분석 시 hold 결정이면 토스 알림을 보내지 않아야 함
        """
        from app.jobs import kis_trading as kis_tasks

        # Mock analyzer result
        mock_stock_analysis_result.decision = "hold"

        with (
            patch("app.jobs.kis_trading.KISClient") as MockKIS,
            patch("app.jobs.kis_trading.KISAnalyzer") as MockAnalyzer,
            patch("app.jobs.kis_trading.get_trade_notifier") as mock_get_notifier,
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.toss_notification_service.send_toss_notification_if_needed"
            ) as mock_send_toss,
        ):
            # Setup mocks
            mock_kis = MockKIS.return_value
            mock_kis.fetch_fundamental_info = AsyncMock(
                return_value={"종목명": "NAVER", "현재가": 180000}
            )

            mock_analyzer_instance = MockAnalyzer.return_value
            mock_analyzer_instance.analyze_stock_json = AsyncMock(
                return_value=(mock_stock_analysis_result, "gemini-2.5-pro")
            )
            mock_analyzer_instance.close = AsyncMock(return_value=None)

            mock_notifier = MagicMock()
            mock_notifier.notify_analysis_complete = AsyncMock(return_value=True)
            mock_get_notifier.return_value = mock_notifier

            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            mock_send_toss.return_value = False

            # Execute
            result = await kis_tasks._analyze_domestic_stock_async("035420")

            # Assertions
            assert result["status"] == "completed"

            # Verify Toss notification was called (but returned False due to hold)
            mock_send_toss.assert_called_once()
            call_kwargs = mock_send_toss.call_args.kwargs
            assert call_kwargs["decision"] == "hold"


class TestOverseasStockTossNotification:
    """해외 주식 자동 실행 시 토스 알림 테스트"""

    @pytest.mark.asyncio
    async def test_analyze_overseas_stock_sends_toss_notification_on_buy(
        self, mock_db, mock_stock_analysis_result
    ):
        """
        해외 주식 분석 시 buy 결정이고 토스 잔고가 있으면 알림을 보내야 함
        """
        from app.jobs import kis_trading as kis_tasks
        from app.models.manual_holdings import MarketType

        # Mock analyzer result
        mock_stock_analysis_result.decision = "buy"

        with (
            patch("app.analysis.service_analyzers.YahooAnalyzer") as MockAnalyzer,
            patch("app.services.yahoo.fetch_price") as mock_fetch_price,
            patch("app.jobs.kis_trading.get_trade_notifier") as mock_get_notifier,
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.toss_notification_service.send_toss_notification_if_needed"
            ) as mock_send_toss,
        ):
            # Setup analyzer mock
            mock_analyzer_instance = MockAnalyzer.return_value
            mock_analyzer_instance.analyze_stock_json = AsyncMock(
                return_value=(mock_stock_analysis_result, "gemini-2.5-pro")
            )
            mock_analyzer_instance.close = AsyncMock(return_value=None)

            # Setup Yahoo price mock (returns DataFrame)
            import pandas as pd

            price_df = pd.DataFrame([{"close": 175.50}])
            mock_fetch_price.return_value = price_df

            # Setup notifier mock
            mock_notifier = MagicMock()
            mock_notifier.notify_analysis_complete = AsyncMock(return_value=True)
            mock_get_notifier.return_value = mock_notifier

            # Setup DB mock
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            # Setup Toss notification mock
            mock_send_toss.return_value = True

            # Execute
            result = await kis_tasks._analyze_overseas_stock_async("AAPL")

            # Assertions
            assert result["status"] == "completed"
            assert result["symbol"] == "AAPL"

            # Verify Toss notification was called
            mock_send_toss.assert_called_once()

            call_kwargs = mock_send_toss.call_args.kwargs
            assert call_kwargs["ticker"] == "AAPL"
            assert call_kwargs["name"] == "AAPL"
            assert call_kwargs["market_type"] == MarketType.US
            assert call_kwargs["decision"] == "buy"

    @pytest.mark.asyncio
    async def test_analyze_overseas_stock_sends_toss_notification_on_sell(
        self, mock_db, mock_stock_analysis_result
    ):
        """
        해외 주식 분석 시 sell 결정이고 토스 잔고가 있으면 알림을 보내야 함
        """
        from app.jobs import kis_trading as kis_tasks

        # Mock analyzer result
        mock_stock_analysis_result.decision = "sell"

        with (
            patch("app.analysis.service_analyzers.YahooAnalyzer") as MockAnalyzer,
            patch("app.services.yahoo.fetch_price") as mock_fetch_price,
            patch("app.jobs.kis_trading.get_trade_notifier") as mock_get_notifier,
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.toss_notification_service.send_toss_notification_if_needed"
            ) as mock_send_toss,
        ):
            # Setup analyzer mock
            mock_analyzer_instance = MockAnalyzer.return_value
            mock_analyzer_instance.analyze_stock_json = AsyncMock(
                return_value=(mock_stock_analysis_result, "gemini-2.5-pro")
            )
            mock_analyzer_instance.close = AsyncMock(return_value=None)

            # Setup Yahoo price mock (returns DataFrame)
            import pandas as pd

            price_df = pd.DataFrame([{"close": 250.00}])
            mock_fetch_price.return_value = price_df

            # Setup notifier mock
            mock_notifier = MagicMock()
            mock_notifier.notify_analysis_complete = AsyncMock(return_value=True)
            mock_get_notifier.return_value = mock_notifier

            # Setup DB mock
            mock_session_instance = MagicMock()
            mock_session_instance.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_instance.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session_instance

            # Setup Toss notification mock
            mock_send_toss.return_value = True

            # Execute
            result = await kis_tasks._analyze_overseas_stock_async("TSLA")

            # Assertions
            assert result["status"] == "completed"

            # Verify Toss notification was called with sell decision
            mock_send_toss.assert_called_once()

            call_kwargs = mock_send_toss.call_args.kwargs
            assert call_kwargs["decision"] == "sell"
            assert (
                call_kwargs["recommended_sell_price"] == 75000
            )  # appropriate_sell_min


class TestTossNotificationIntegration:
    """통합 시나리오 테스트"""

    @pytest.mark.asyncio
    async def test_domestic_stock_analysis_sends_toss_notification(self, mock_db):
        """
        국내 주식 단일 분석 시 토스 알림이 전송되는지 확인
        """
        from app.jobs import kis_trading as kis_tasks
        from app.models.analysis import StockAnalysisResult
        from app.models.manual_holdings import MarketType

        # Create result with all required fields
        result = StockAnalysisResult(
            decision="buy",
            confidence=80,
            appropriate_buy_min=68000,
            appropriate_buy_max=70000,
            appropriate_sell_min=75000,
            appropriate_sell_max=77000,
            buy_hope_min=65000,
            buy_hope_max=67000,
            sell_target_min=80000,
            sell_target_max=82000,
            model_name="gemini-2.5-pro",
            prompt="test",
            reasons=["기술적 분석 상 매수 시점"],
        )

        with (
            patch("app.jobs.kis_trading.KISClient") as MockKIS,
            patch("app.jobs.kis_trading.KISAnalyzer") as MockAnalyzer,
            patch("app.jobs.kis_trading.get_trade_notifier") as mock_notifier,
            patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
            patch(
                "app.services.toss_notification_service.send_toss_notification_if_needed"
            ) as mock_send_toss,
        ):
            # Setup mocks
            mock_kis = MockKIS.return_value
            mock_kis.fetch_fundamental_info = AsyncMock(
                return_value={"종목명": "삼성전자", "현재가": 70000}
            )

            mock_analyzer = MockAnalyzer.return_value
            mock_analyzer.analyze_stock_json = AsyncMock(
                return_value=(result, "gemini-2.5-pro")
            )
            mock_analyzer.close = AsyncMock(return_value=None)

            mock_notifier_instance = MagicMock()
            mock_notifier_instance.notify_analysis_complete = AsyncMock(
                return_value=True
            )
            mock_notifier.return_value = mock_notifier_instance

            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session

            mock_send_toss.return_value = True

            # Execute
            analysis_result = await kis_tasks._analyze_domestic_stock_async("005930")

            # Assertions
            assert analysis_result["status"] == "completed"

            # Verify Toss notification was called
            mock_send_toss.assert_called_once()

            call_kwargs = mock_send_toss.call_args.kwargs
            assert call_kwargs["ticker"] == "005930"
            assert call_kwargs["name"] == "삼성전자"
            assert call_kwargs["market_type"] == MarketType.KR
            assert call_kwargs["decision"] == "buy"
            assert call_kwargs["current_price"] == 70000.0
            assert call_kwargs["recommended_buy_price"] == 68000
