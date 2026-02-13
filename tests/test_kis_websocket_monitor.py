"""
Unit tests for KIS WebSocket monitor

Tests for KIS WebSocket monitor including:
- Signal handling (SIGINT, SIGTERM)
- DCA integration (step update, next step)
- Execution event publishing
- Graceful shutdown
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import patch

from kis_websocket_monitor import KISWebSocketMonitor


@pytest.mark.unit
class TestKISWebSocketMonitorInit:
    """Tests for monitor initialization"""

    def test_monitor_initialization(self):
        """모니터 초기화 테스트"""
        monitor = KISWebSocketMonitor()

        assert monitor.dca_service is None
        assert monitor.websocket_client is None
        assert monitor.is_running is False

    def test_signal_handlers_installed(self):
        """시그널 핸들러 설치 테스트"""
        import signal

        monitor = KISWebSocketMonitor()

        with patch.object(signal, "signal") as mock_signal:
            monitor._setup_signal_handlers()

            mock_signal.assert_called()
            args = mock_signal.call_args_list
            assert len(args) == 2
            assert args[0][0][0] in (signal.SIGINT, signal.SIGTERM)
            assert args[1][0][0] in (signal.SIGINT, signal.SIGTERM)


@pytest.mark.unit
class TestKISWebSocketMonitorDCAIntegration:
    """Tests for DCA service integration"""

    @pytest.mark.asyncio
    async def test_on_execution_without_order_id(self):
        """order_id 없는 체결 이벤트 처리 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.dca_service = AsyncMock()
        monitor.websocket_client = AsyncMock()

        mock_publish = AsyncMock()
        with patch("kis_websocket_monitor.publish_execution_event", mock_publish):
            event = {
                "type": "execution",
                "market": "kr",
                "symbol": "005930",
                "filled_price": 49500,
                "filled_qty": 0.001,
            }

            await monitor._on_execution(event)

            monitor.dca_service.find_step_by_order_id.assert_not_called()
            monitor.dca_service.mark_step_filled.assert_not_called()
            mock_publish.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_on_execution_with_matching_order_id(self):
        """order_id 매칭 성공 DCA 업데이트 테스트"""
        monitor = KISWebSocketMonitor()
        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.plan_id = 1
        mock_step.step_number = 1

        monitor.dca_service = AsyncMock()
        monitor.dca_service.find_step_by_order_id = AsyncMock(return_value=mock_step)
        monitor.dca_service.mark_step_filled = AsyncMock()
        monitor.dca_service.get_next_pending_step = AsyncMock(return_value=None)

        mock_publish = AsyncMock()
        with patch("kis_websocket_monitor.publish_execution_event", mock_publish):
            event = {
                "type": "execution",
                "market": "kr",
                "symbol": "005930",
                "order_id": "ORDER-123",
                "filled_price": 49500,
                "filled_qty": 0.001,
            }

            await monitor._on_execution(event)

            monitor.dca_service.find_step_by_order_id.assert_called_once_with(
                "ORDER-123"
            )
            monitor.dca_service.mark_step_filled.assert_called_once_with(
                step_id=1,
                filled_price=Decimal("49500"),
                filled_qty=Decimal("0.001"),
            )

    @pytest.mark.asyncio
    async def test_on_execution_with_next_pending_step(self):
        """다음 pending step 있는 이벤트 발행 테스트"""
        monitor = KISWebSocketMonitor()
        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.plan_id = 1
        mock_step.step_number = 1

        mock_next_step = MagicMock()
        mock_next_step.plan_id = 1
        mock_next_step.step_number = 2
        mock_next_step.target_price = Decimal("49000.00")
        mock_next_step.target_quantity = Decimal("0.002")

        monitor.dca_service = AsyncMock()
        monitor.dca_service.find_step_by_order_id = AsyncMock(return_value=mock_step)
        monitor.dca_service.mark_step_filled = AsyncMock()
        monitor.dca_service.get_next_pending_step = AsyncMock(
            return_value=mock_next_step
        )

        mock_publish = AsyncMock()
        with patch("kis_websocket_monitor.publish_execution_event", mock_publish):
            event = {
                "type": "execution",
                "market": "kr",
                "symbol": "005930",
                "order_id": "ORDER-123",
                "filled_price": 49500,
                "filled_qty": 0.001,
            }

            await monitor._on_execution(event)

            assert "dca_next_step" in event
            assert event["dca_next_step"]["plan_id"] == 1
            assert event["dca_next_step"]["step_number"] == 2
            assert event["dca_next_step"]["target_price"] == 49000.0
            assert event["dca_next_step"]["target_quantity"] == "0.002"

            mock_publish.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_on_execution_dca_failure_continues(self):
        """DCA 조회 실패 시에도 이벤트 발행 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.dca_service = AsyncMock()
        monitor.dca_service.find_step_by_order_id = AsyncMock(return_value=None)

        mock_publish = AsyncMock()
        with patch("kis_websocket_monitor.publish_execution_event", mock_publish):
            event = {
                "type": "execution",
                "market": "kr",
                "order_id": "ORDER-123",
            }

            await monitor._on_execution(event)

            monitor.dca_service.mark_step_filled.assert_not_called()
            mock_publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_dca_step_without_service(self):
        """DCA 서비스 없는 경우 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.dca_service = None

        event = {
            "type": "execution",
            "market": "kr",
            "order_id": "ORDER-123",
        }

        await monitor._update_dca_step("ORDER-123", event)

        assert True


@pytest.mark.unit
class TestKISWebSocketMonitorSignalHandling:
    """Tests for signal handling"""

    def test_handle_sigint(self):
        """SIGINT 시그널 처리 테스트"""
        import signal

        monitor = KISWebSocketMonitor()
        monitor.is_running = True

        monitor._handle_signal(signal.SIGINT, None)

        assert monitor.is_running is False

    def test_handle_sigterm(self):
        """SIGTERM 시그널 처리 테스트"""
        import signal

        monitor = KISWebSocketMonitor()
        monitor.is_running = True

        monitor._handle_signal(signal.SIGTERM, None)

        assert monitor.is_running is False


@pytest.mark.unit
class TestKISWebSocketMonitorStartStop:
    """Tests for monitor start/stop lifecycle"""

    @pytest.mark.asyncio
    async def test_start_initializes_services(self):
        """모니터 시작 시 서비스 초기화 테스트"""
        monitor = KISWebSocketMonitor()

        with patch.object(monitor, "_initialize_db") as mock_init_db:
            mock_session = AsyncMock(return_value=None)
            mock_session.__aenter__ = AsyncMock(return_value=None)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_init_db.return_value = mock_session

            with patch.object(monitor, "_initialize_dca_service"):
                with patch.object(monitor, "_initialize_websocket"):
                    mock_ws = AsyncMock()
                    monitor.websocket_client = mock_ws
                    mock_ws.connect_and_subscribe = AsyncMock()
                    mock_ws.listen = AsyncMock()

                    await monitor.start()

                    monitor.dca_service is not None
                    mock_ws.connect_and_subscribe.assert_called_once()
                    mock_ws.listen.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_websocket(self):
        """WebSocket 정지 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.is_running = True

        mock_ws_client = AsyncMock()
        mock_ws_client.stop = AsyncMock()
        monitor.websocket_client = mock_ws_client

        with patch("kis_websocket_monitor.close_execution_redis") as mock_close_redis:
            mock_close_redis.return_value = AsyncMock()

            await monitor.stop()

            assert monitor.is_running is False
            mock_ws_client.stop.assert_called_once()
            mock_close_redis.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        """이미 정지된 상태에서 stop 호출 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.is_running = False
        monitor.websocket_client = None

        with patch("kis_websocket_monitor.close_execution_redis") as mock_close_redis:
            mock_close_redis.return_value = AsyncMock()

            await monitor.stop()

            mock_close_redis.assert_called_once()
