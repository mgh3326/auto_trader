"""
Unit tests for KIS WebSocket monitor

Tests for KIS WebSocket monitor including:
- Signal handling (SIGINT, SIGTERM)
- DCA integration (step update, next step)
- Execution event publishing
- Graceful shutdown
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kis_websocket_monitor import KISWebSocketMonitor, main


@pytest.mark.unit
class TestKISWebSocketMonitorInit:
    """Tests for monitor initialization"""

    def test_monitor_initialization(self):
        """모니터 초기화 테스트"""
        monitor = KISWebSocketMonitor()

        assert monitor.dca_service is None
        assert monitor.websocket_client is None
        assert monitor._db_engine is None
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

    def test_initialize_db_returns_session_factory(self):
        """_initialize_db가 호출 가능한 세션 팩토리를 반환하는지 테스트"""
        monitor = KISWebSocketMonitor()
        mock_engine = MagicMock()

        with patch("kis_websocket_monitor.create_async_engine", return_value=mock_engine):
            session_factory = monitor._initialize_db()

        assert callable(session_factory)
        assert monitor._db_engine is mock_engine

    @pytest.mark.asyncio
    async def test_start_initializes_services(self):
        """모니터 시작 시 서비스 초기화 테스트"""
        monitor = KISWebSocketMonitor()

        mock_db_session = AsyncMock()

        class MockSessionMaker:
            """Mock that is both callable and async context manager"""

            def __call__(self):
                return self

            async def __aenter__(self):
                return mock_db_session

            async def __aexit__(self, *args):
                return None

        mock_session_maker = MockSessionMaker()

        def mock_init_db():
            return mock_session_maker

        with patch.object(monitor, "_initialize_db", side_effect=mock_init_db):
            with patch.object(monitor, "_initialize_dca_service"):
                with patch.object(monitor, "_initialize_websocket"):
                    mock_ws = AsyncMock()
                    monitor.websocket_client = mock_ws
                    mock_ws.connect_and_subscribe = AsyncMock()
                    mock_ws.listen = AsyncMock()

                    await monitor.start()

                    mock_ws.connect_and_subscribe.assert_called_once()
                    mock_ws.listen.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_raises_when_websocket_not_initialized(self):
        """웹소켓 초기화 실패 시 RuntimeError 발생 테스트"""
        monitor = KISWebSocketMonitor()

        mock_db_session = AsyncMock()

        class MockSessionMaker:
            def __call__(self):
                return self

            async def __aenter__(self):
                return mock_db_session

            async def __aexit__(self, *args):
                return None

        with (
            patch.object(monitor, "_initialize_db", return_value=MockSessionMaker()),
            patch.object(monitor, "_initialize_dca_service"),
            patch.object(monitor, "_initialize_websocket"),
        ):
            monitor.websocket_client = None
            with pytest.raises(
                RuntimeError, match="KIS WebSocket client initialization failed"
            ):
                await monitor.start()

    @pytest.mark.asyncio
    async def test_stop_websocket(self):
        """WebSocket 정지 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.is_running = True

        mock_ws_client = AsyncMock()
        mock_ws_client.stop = AsyncMock()
        monitor.websocket_client = mock_ws_client

        mock_close_redis = AsyncMock()
        with patch(
            "kis_websocket_monitor.close_execution_redis",
            new=mock_close_redis,
        ):

            await monitor.stop()

            assert monitor.is_running is False
            mock_ws_client.stop.assert_awaited_once()
            mock_close_redis.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_disposes_db_engine(self):
        """DB 엔진 dispose 호출 테스트"""
        monitor = KISWebSocketMonitor()
        mock_engine = AsyncMock()
        monitor._db_engine = mock_engine

        mock_close_redis = AsyncMock()
        with patch("kis_websocket_monitor.close_execution_redis", new=mock_close_redis):
            await monitor.stop()

        mock_engine.dispose.assert_awaited_once()
        assert monitor._db_engine is None
        mock_close_redis.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        """이미 정지된 상태에서 stop 호출 테스트"""
        monitor = KISWebSocketMonitor()
        monitor.is_running = False
        monitor.websocket_client = None

        mock_close_redis = AsyncMock()
        with patch(
            "kis_websocket_monitor.close_execution_redis",
            new=mock_close_redis,
        ):

            await monitor.stop()

            mock_close_redis.assert_awaited_once()


@pytest.mark.unit
class TestKISWebSocketMonitorSentry:
    """Tests for Sentry capture integration."""

    @pytest.mark.asyncio
    async def test_main_captures_fatal_exception(self):
        mock_monitor = AsyncMock()
        mock_monitor.start = AsyncMock(side_effect=RuntimeError("fatal"))
        mock_monitor.stop = AsyncMock()

        with (
            patch("kis_websocket_monitor.init_sentry") as mock_init_sentry,
            patch("kis_websocket_monitor.capture_exception") as mock_capture_exception,
            patch("kis_websocket_monitor.KISWebSocketMonitor", return_value=mock_monitor),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await main()

        assert exc_info.value.code == 1
        mock_init_sentry.assert_called_once_with(service_name="auto-trader-kis-ws")
        mock_capture_exception.assert_called_once()
        mock_monitor.stop.assert_awaited_once()
