"""
Tests for Toss notification integration in KIS automation tasks.

종목별 자동 실행 시 분석 함수가 no-op 상태임을 확인하는 테스트.
(Gemini analyzer 제거 후, OpenClaw 기반 대체 전까지 no-op)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    return AsyncMock()


class TestDomesticStockTossNotification:
    """국내 주식 분석 함수 no-op 테스트"""

    @pytest.mark.asyncio
    async def test_analyze_domestic_stock_returns_ignored_on_buy_scenario(self, mock_db):
        """국내 주식 분석 함수는 no-op으로 ignored 상태를 반환해야 함"""
        from app.jobs import kis_trading as kis_tasks

        with patch(
            "app.services.toss_notification_service.send_toss_notification_if_needed"
        ) as mock_send_toss:
            result = await kis_tasks._analyze_domestic_stock_async("005930")

            assert result["status"] == "ignored"
            assert result["symbol"] == "005930"
            mock_send_toss.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_domestic_stock_returns_ignored_on_sell_scenario(self, mock_db):
        """국내 주식 분석 함수는 no-op으로 ignored 상태를 반환해야 함"""
        from app.jobs import kis_trading as kis_tasks

        with patch(
            "app.services.toss_notification_service.send_toss_notification_if_needed"
        ) as mock_send_toss:
            result = await kis_tasks._analyze_domestic_stock_async("035720")

            assert result["status"] == "ignored"
            mock_send_toss.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_domestic_stock_no_toss_notification_on_hold(self, mock_db):
        """국내 주식 분석 함수는 no-op이므로 토스 알림을 보내지 않아야 함"""
        from app.jobs import kis_trading as kis_tasks

        with patch(
            "app.services.toss_notification_service.send_toss_notification_if_needed"
        ) as mock_send_toss:
            result = await kis_tasks._analyze_domestic_stock_async("035420")

            assert result["status"] == "ignored"
            mock_send_toss.assert_not_called()


class TestOverseasStockTossNotification:
    """해외 주식 분석 함수 no-op 테스트"""

    @pytest.mark.asyncio
    async def test_analyze_overseas_stock_returns_ignored_on_buy_scenario(self, mock_db):
        """해외 주식 분석 함수는 no-op으로 ignored 상태를 반환해야 함"""
        from app.jobs import kis_trading as kis_tasks

        with patch(
            "app.services.toss_notification_service.send_toss_notification_if_needed"
        ) as mock_send_toss:
            result = await kis_tasks._analyze_overseas_stock_async("AAPL")

            assert result["status"] == "ignored"
            assert result["symbol"] == "AAPL"
            mock_send_toss.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_overseas_stock_returns_ignored_on_sell_scenario(self, mock_db):
        """해외 주식 분석 함수는 no-op으로 ignored 상태를 반환해야 함"""
        from app.jobs import kis_trading as kis_tasks

        with patch(
            "app.services.toss_notification_service.send_toss_notification_if_needed"
        ) as mock_send_toss:
            result = await kis_tasks._analyze_overseas_stock_async("TSLA")

            assert result["status"] == "ignored"
            mock_send_toss.assert_not_called()


class TestTossNotificationIntegration:
    """통합 시나리오 테스트"""

    @pytest.mark.asyncio
    async def test_domestic_stock_analysis_sends_toss_notification(self, mock_db):
        """국내 주식 단일 분석 시 no-op이므로 토스 알림 미발송"""
        from app.jobs import kis_trading as kis_tasks

        with patch(
            "app.services.toss_notification_service.send_toss_notification_if_needed"
        ) as mock_send_toss:
            analysis_result = await kis_tasks._analyze_domestic_stock_async("005930")

            assert analysis_result["status"] == "ignored"
            mock_send_toss.assert_not_called()
