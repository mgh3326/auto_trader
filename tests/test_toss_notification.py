"""
Tests for Toss Notification Service

토스 보유 종목 알림 기능 테스트:
- 토스만 있는 경우 알림 발송 확인
- 한투+토스 둘 다 있는 경우에도 알림 발송 확인
- 한투만 있는 경우(toss_quantity == 0) 알림 안 함 확인
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.manual_holdings import MarketType
from app.monitoring.trade_notifier import TradeNotifier, get_trade_notifier
from app.services.merged_portfolio_service import ReferencePrices
from app.services.toss_notification_service import (
    TossNotificationData,
    TossNotificationService,
    send_toss_notification_if_needed,
)


# Note: MarketType values are KR and US (not DOMESTIC)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_trade_notifier():
    """Create a mock TradeNotifier."""
    notifier = MagicMock(spec=TradeNotifier)
    notifier.notify_toss_buy_recommendation = AsyncMock(return_value=True)
    notifier.notify_toss_sell_recommendation = AsyncMock(return_value=True)
    return notifier


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    return AsyncMock()


@pytest.fixture
def toss_only_ref():
    """ReferencePrices with only Toss holdings."""
    return ReferencePrices(
        kis_avg=None,
        kis_quantity=0,
        toss_avg=50000.0,
        toss_quantity=10,
        combined_avg=50000.0,
        total_quantity=10,
    )


@pytest.fixture
def kis_and_toss_ref():
    """ReferencePrices with both KIS and Toss holdings."""
    return ReferencePrices(
        kis_avg=48000.0,
        kis_quantity=5,
        toss_avg=52000.0,
        toss_quantity=10,
        combined_avg=50666.67,
        total_quantity=15,
    )


@pytest.fixture
def kis_only_ref():
    """ReferencePrices with only KIS holdings (no Toss)."""
    return ReferencePrices(
        kis_avg=48000.0,
        kis_quantity=5,
        toss_avg=None,
        toss_quantity=0,
        combined_avg=48000.0,
        total_quantity=5,
    )


# =============================================================================
# TradeNotifier Tests - Message Formatting
# =============================================================================

class TestTradeNotifierFormatting:
    """Test message formatting for Toss notifications."""

    def test_format_toss_buy_recommendation_toss_only(self):
        """Test buy recommendation format with only Toss holdings."""
        notifier = TradeNotifier()
        message = notifier._format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
        )

        assert "📈" in message
        assert "토스 수동매수" in message
        assert "삼성전자" in message
        assert "70,000원" in message  # current price
        assert "토스 보유:" in message
        assert "10주" in message
        assert "65,000원" in message  # toss avg
        assert "추천 매수가:" in message
        assert "68,000원" in message
        assert "5주" in message
        assert "한투 보유:" not in message  # KIS should not appear

    def test_format_toss_buy_recommendation_with_kis(self):
        """Test buy recommendation format with both KIS and Toss holdings."""
        notifier = TradeNotifier()
        message = notifier._format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=5,
            kis_avg_price=63000,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
        )

        assert "토스 보유:" in message
        assert "한투 보유:" in message
        assert "5주" in message  # KIS quantity
        assert "63,000원" in message  # KIS avg

    def test_format_toss_sell_recommendation_toss_only(self):
        """Test sell recommendation format with only Toss holdings."""
        notifier = TradeNotifier()
        message = notifier._format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=35000,
            profit_percent=10.77,
            currency="원",
            market_type="국내주식",
        )

        assert "📉" in message
        assert "토스 수동매도" in message
        assert "삼성전자" in message
        assert "추천 매도가:" in message
        assert "72,000원" in message
        assert "+10.8%" in message
        assert "예상 수익:" in message
        assert "35,000원" in message
        assert "한투 보유:" not in message

    def test_format_toss_sell_recommendation_with_kis(self):
        """Test sell recommendation format with both KIS and Toss holdings."""
        notifier = TradeNotifier()
        message = notifier._format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=5,
            kis_avg_price=63000,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=35000,
            profit_percent=10.77,
            currency="원",
            market_type="국내주식",
        )

        assert "토스 보유:" in message
        assert "한투 보유:" in message

    def test_format_toss_buy_recommendation_usd(self):
        """Test buy recommendation format for US stocks (USD)."""
        notifier = TradeNotifier()
        message = notifier._format_toss_buy_recommendation(
            symbol="AAPL",
            korean_name="애플",
            current_price=175.50,
            toss_quantity=10,
            toss_avg_price=165.00,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=170.00,
            recommended_quantity=5,
            currency="$",
            market_type="해외주식",
        )

        assert "$175.50" in message
        assert "$165.00" in message
        assert "$170.00" in message

    def test_format_toss_sell_recommendation_negative_profit(self):
        """Test sell recommendation format with negative profit."""
        notifier = TradeNotifier()
        message = notifier._format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=60000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=62000,
            recommended_quantity=5,
            expected_profit=-15000,
            profit_percent=-4.62,
            currency="원",
            market_type="국내주식",
        )

        # Negative profit should not have + sign
        assert "-4.6%" in message


# =============================================================================
# TradeNotifier Tests - Notification Sending
# =============================================================================

class TestTradeNotifierSending:
    """Test notification sending logic for Toss notifications."""

    @pytest.mark.asyncio
    async def test_notify_toss_buy_skips_when_no_toss_holdings(self):
        """Test that buy notification is skipped when toss_quantity = 0."""
        notifier = TradeNotifier()
        notifier._enabled = True

        result = await notifier.notify_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=0,  # No Toss holdings
            toss_avg_price=0,
            kis_quantity=5,
            kis_avg_price=63000,
            recommended_price=68000,
            recommended_quantity=5,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_toss_sell_skips_when_no_toss_holdings(self):
        """Test that sell notification is skipped when toss_quantity = 0."""
        notifier = TradeNotifier()
        notifier._enabled = True

        result = await notifier.notify_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=0,  # No Toss holdings
            toss_avg_price=0,
            kis_quantity=5,
            kis_avg_price=63000,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=0,
            profit_percent=0,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_toss_buy_skips_when_disabled(self):
        """Test that buy notification is skipped when notifier is disabled."""
        notifier = TradeNotifier()
        notifier._enabled = False

        result = await notifier.notify_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=68000,
            recommended_quantity=5,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_toss_sell_skips_when_disabled(self):
        """Test that sell notification is skipped when notifier is disabled."""
        notifier = TradeNotifier()
        notifier._enabled = False

        result = await notifier.notify_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=35000,
            profit_percent=10.77,
        )

        assert result is False


# =============================================================================
# TossNotificationData Tests
# =============================================================================

class TestTossNotificationData:
    """Test TossNotificationData dataclass."""

    def test_default_values(self):
        """Test default values for TossNotificationData."""
        data = TossNotificationData(
            ticker="005930",
            name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
        )

        assert data.kis_quantity is None
        assert data.kis_avg_price is None
        assert data.recommended_price == 0.0
        assert data.recommended_quantity == 1
        assert data.expected_profit == 0.0
        assert data.profit_percent == 0.0
        assert data.currency == "원"
        assert data.market_type == "국내주식"

    def test_custom_values(self):
        """Test custom values for TossNotificationData."""
        data = TossNotificationData(
            ticker="AAPL",
            name="애플",
            current_price=175.50,
            toss_quantity=10,
            toss_avg_price=165.00,
            kis_quantity=5,
            kis_avg_price=160.00,
            recommended_price=170.00,
            recommended_quantity=3,
            expected_profit=150.00,
            profit_percent=3.03,
            currency="$",
            market_type="해외주식",
        )

        assert data.ticker == "AAPL"
        assert data.kis_quantity == 5
        assert data.currency == "$"
        assert data.market_type == "해외주식"


# =============================================================================
# TossNotificationService Tests
# =============================================================================

class TestTossNotificationService:
    """Test TossNotificationService class."""

    @pytest.mark.asyncio
    async def test_should_notify_toss_with_toss_holdings(self, mock_db, toss_only_ref):
        """Test should_notify_toss returns True when Toss holdings exist."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=toss_only_ref),
        ):
            should_notify, ref = await service.should_notify_toss(
                user_id=1,
                ticker="005930",
                market_type=MarketType.KR,
            )

        assert should_notify is True
        assert ref is not None
        assert ref.toss_quantity == 10

    @pytest.mark.asyncio
    async def test_should_notify_toss_without_toss_holdings(self, mock_db, kis_only_ref):
        """Test should_notify_toss returns False when no Toss holdings."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=kis_only_ref),
        ):
            should_notify, ref = await service.should_notify_toss(
                user_id=1,
                ticker="005930",
                market_type=MarketType.KR,
            )

        assert should_notify is False
        assert ref is None

    @pytest.mark.asyncio
    async def test_notify_buy_recommendation_sends_when_toss_exists(
        self, mock_db, mock_trade_notifier
    ):
        """Test buy notification is sent when Toss holdings exist."""
        service = TossNotificationService(mock_db)

        data = TossNotificationData(
            ticker="005930",
            name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            recommended_price=68000,
            recommended_quantity=5,
        )

        with patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.notify_buy_recommendation(data)

        assert result is True
        mock_trade_notifier.notify_toss_buy_recommendation.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_buy_recommendation_skips_when_no_toss(
        self, mock_db, mock_trade_notifier
    ):
        """Test buy notification is skipped when no Toss holdings."""
        service = TossNotificationService(mock_db)

        data = TossNotificationData(
            ticker="005930",
            name="삼성전자",
            current_price=70000,
            toss_quantity=0,  # No Toss holdings
            toss_avg_price=0,
        )

        with patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.notify_buy_recommendation(data)

        assert result is False
        mock_trade_notifier.notify_toss_buy_recommendation.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_sell_recommendation_sends_when_toss_exists(
        self, mock_db, mock_trade_notifier
    ):
        """Test sell notification is sent when Toss holdings exist."""
        service = TossNotificationService(mock_db)

        data = TossNotificationData(
            ticker="005930",
            name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=35000,
            profit_percent=10.77,
        )

        with patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.notify_sell_recommendation(data)

        assert result is True
        mock_trade_notifier.notify_toss_sell_recommendation.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_sell_recommendation_skips_when_no_toss(
        self, mock_db, mock_trade_notifier
    ):
        """Test sell notification is skipped when no Toss holdings."""
        service = TossNotificationService(mock_db)

        data = TossNotificationData(
            ticker="005930",
            name="삼성전자",
            current_price=70000,
            toss_quantity=0,  # No Toss holdings
            toss_avg_price=0,
        )

        with patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.notify_sell_recommendation(data)

        assert result is False
        mock_trade_notifier.notify_toss_sell_recommendation.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_analysis_result_buy_with_toss(
        self, mock_db, toss_only_ref, mock_trade_notifier
    ):
        """Test process_analysis_result sends buy notification when Toss exists."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=toss_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is True
        mock_trade_notifier.notify_toss_buy_recommendation.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_analysis_result_sell_with_toss(
        self, mock_db, toss_only_ref, mock_trade_notifier
    ):
        """Test process_analysis_result sends sell notification when Toss exists."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=toss_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="sell",
                current_price=72000,
                recommended_sell_price=72000,
                recommended_quantity=5,
            )

        assert result is True
        mock_trade_notifier.notify_toss_sell_recommendation.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_analysis_result_hold_no_notification(
        self, mock_db, toss_only_ref, mock_trade_notifier
    ):
        """Test process_analysis_result sends no notification for hold decision."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=toss_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="hold",
                current_price=70000,
            )

        assert result is False
        mock_trade_notifier.notify_toss_buy_recommendation.assert_not_called()
        mock_trade_notifier.notify_toss_sell_recommendation.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_analysis_result_buy_without_toss(
        self, mock_db, kis_only_ref, mock_trade_notifier
    ):
        """Test process_analysis_result sends no notification when no Toss holdings."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=kis_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is False
        mock_trade_notifier.notify_toss_buy_recommendation.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_analysis_result_with_kis_and_toss(
        self, mock_db, kis_and_toss_ref, mock_trade_notifier
    ):
        """Test process_analysis_result sends notification when both KIS and Toss exist."""
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=kis_and_toss_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is True
        mock_trade_notifier.notify_toss_buy_recommendation.assert_called_once()

        # Verify both KIS and Toss info was passed
        call_kwargs = mock_trade_notifier.notify_toss_buy_recommendation.call_args.kwargs
        assert call_kwargs["toss_quantity"] == 10
        assert call_kwargs["kis_quantity"] == 5

    @pytest.mark.asyncio
    async def test_process_analysis_result_us_market(
        self, mock_db, mock_trade_notifier
    ):
        """Test process_analysis_result handles US market correctly."""
        us_ref = ReferencePrices(
            kis_avg=None,
            kis_quantity=0,
            toss_avg=165.00,
            toss_quantity=10,
            combined_avg=165.00,
            total_quantity=10,
        )

        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            return_value=us_ref,
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="AAPL",
                name="애플",
                market_type=MarketType.US,
                decision="buy",
                current_price=175.50,
                recommended_buy_price=170.00,
                recommended_quantity=3,
            )

        assert result is True

        # Verify USD currency was used
        call_kwargs = mock_trade_notifier.notify_toss_buy_recommendation.call_args.kwargs
        assert call_kwargs["currency"] == "$"
        assert call_kwargs["market_type"] == "해외주식"


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestSendTossNotificationIfNeeded:
    """Test send_toss_notification_if_needed helper function."""

    @pytest.mark.asyncio
    async def test_helper_function_with_toss_holdings(
        self, mock_db, toss_only_ref, mock_trade_notifier
    ):
        """Test helper function sends notification when Toss holdings exist."""
        with patch(
            "app.services.toss_notification_service.TossNotificationService"
        ) as MockService:
            mock_service_instance = MockService.return_value
            mock_service_instance.process_analysis_result = AsyncMock(return_value=True)

            result = await send_toss_notification_if_needed(
                db=mock_db,
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is True
        mock_service_instance.process_analysis_result.assert_called_once()

    @pytest.mark.asyncio
    async def test_helper_function_without_toss_holdings(
        self, mock_db, kis_only_ref, mock_trade_notifier
    ):
        """Test helper function returns False when no Toss holdings."""
        with patch(
            "app.services.toss_notification_service.TossNotificationService"
        ) as MockService:
            mock_service_instance = MockService.return_value
            mock_service_instance.process_analysis_result = AsyncMock(return_value=False)

            result = await send_toss_notification_if_needed(
                db=mock_db,
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is False


# =============================================================================
# Integration Scenarios
# =============================================================================

class TestIntegrationScenarios:
    """Test complete notification scenarios."""

    @pytest.mark.asyncio
    async def test_scenario_toss_only_buy_recommendation(
        self, mock_db, toss_only_ref, mock_trade_notifier
    ):
        """
        Scenario: User has holdings only in Toss, AI recommends buy.
        Expected: Notification should be sent with only Toss info.
        """
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=toss_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is True
        call_kwargs = mock_trade_notifier.notify_toss_buy_recommendation.call_args.kwargs
        assert call_kwargs["toss_quantity"] == 10
        assert call_kwargs["kis_quantity"] is None

    @pytest.mark.asyncio
    async def test_scenario_kis_and_toss_sell_recommendation(
        self, mock_db, kis_and_toss_ref, mock_trade_notifier
    ):
        """
        Scenario: User has holdings in both KIS and Toss, AI recommends sell.
        Expected: Notification should be sent with both KIS and Toss info.
        """
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=kis_and_toss_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="sell",
                current_price=72000,
                recommended_sell_price=72000,
                recommended_quantity=5,
            )

        assert result is True
        call_kwargs = mock_trade_notifier.notify_toss_sell_recommendation.call_args.kwargs
        assert call_kwargs["toss_quantity"] == 10
        assert call_kwargs["kis_quantity"] == 5
        assert call_kwargs["toss_avg_price"] == 52000.0
        assert call_kwargs["kis_avg_price"] == 48000.0

    @pytest.mark.asyncio
    async def test_scenario_kis_only_no_notification(
        self, mock_db, kis_only_ref, mock_trade_notifier
    ):
        """
        Scenario: User has holdings only in KIS (no Toss), AI recommends buy.
        Expected: No notification should be sent.
        """
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=kis_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="buy",
                current_price=70000,
                recommended_buy_price=68000,
                recommended_quantity=5,
            )

        assert result is False
        mock_trade_notifier.notify_toss_buy_recommendation.assert_not_called()
        mock_trade_notifier.notify_toss_sell_recommendation.assert_not_called()

    @pytest.mark.asyncio
    async def test_scenario_profit_calculation_on_sell(
        self, mock_db, toss_only_ref, mock_trade_notifier
    ):
        """
        Scenario: User has Toss holdings, AI recommends sell with profit.
        Expected: Correct profit percentage and amount calculated.
        """
        service = TossNotificationService(mock_db)

        with patch.object(
            service.portfolio_service,
            "get_reference_prices",
            new=AsyncMock(return_value=toss_only_ref),
        ), patch(
            "app.services.toss_notification_service.get_trade_notifier",
            return_value=mock_trade_notifier,
        ):
            # Toss avg is 50000, sell at 55000 = 10% profit
            result = await service.process_analysis_result(
                user_id=1,
                ticker="005930",
                name="삼성전자",
                market_type=MarketType.KR,
                decision="sell",
                current_price=55000,
                recommended_sell_price=55000,
                recommended_quantity=5,
            )

        assert result is True
        call_kwargs = mock_trade_notifier.notify_toss_sell_recommendation.call_args.kwargs
        # Expected profit: (55000 - 50000) / 50000 * 100 = 10%
        assert call_kwargs["profit_percent"] == 10.0
        # Expected amount: (55000 - 50000) * 5 = 25000
        assert call_kwargs["expected_profit"] == 25000.0
