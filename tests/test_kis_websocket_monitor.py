from unittest.mock import AsyncMock, patch

import pytest

from kis_websocket_monitor import KISWebSocketMonitor, main


@pytest.mark.unit
class TestKISWebSocketMonitorInit:
    def test_monitor_initialization(self):
        monitor = KISWebSocketMonitor()

        assert monitor.websocket_client is None
        assert monitor.is_running is False

    def test_signal_handlers_installed(self):
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
class TestKISWebSocketMonitorExecutionHandling:
    @pytest.mark.asyncio
    async def test_on_execution_without_order_id(self):
        monitor = KISWebSocketMonitor()

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

            mock_publish.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_on_execution_with_order_id_publishes_without_dca_lookup(self):
        monitor = KISWebSocketMonitor()

        mock_publish = AsyncMock()
        with patch("kis_websocket_monitor.publish_execution_event", mock_publish):
            event = {"type": "execution", "market": "kr", "order_id": "ORDER-123"}
            await monitor._on_execution(event)

        mock_publish.assert_awaited_once_with(event)
        assert "dca_next_step" not in event


@pytest.mark.unit
class TestKISWebSocketMonitorSignalHandling:
    def test_handle_sigint(self):
        import signal

        monitor = KISWebSocketMonitor()
        monitor.is_running = True

        monitor._handle_signal(signal.SIGINT, None)

        assert monitor.is_running is False

    def test_handle_sigterm(self):
        import signal

        monitor = KISWebSocketMonitor()
        monitor.is_running = True

        monitor._handle_signal(signal.SIGTERM, None)

        assert monitor.is_running is False


@pytest.mark.unit
class TestKISWebSocketMonitorStartStop:
    @pytest.mark.asyncio
    async def test_start_initializes_websocket(self):
        monitor = KISWebSocketMonitor()

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
        monitor = KISWebSocketMonitor()

        with patch.object(monitor, "_initialize_websocket"):
            monitor.websocket_client = None
            with pytest.raises(
                RuntimeError, match="KIS WebSocket client initialization failed"
            ):
                await monitor.start()

    @pytest.mark.asyncio
    async def test_stop_websocket(self):
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
    async def test_stop_when_not_running(self):
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
    @pytest.mark.asyncio
    async def test_main_captures_fatal_exception(self):
        mock_monitor = AsyncMock()
        mock_monitor.start = AsyncMock(side_effect=RuntimeError("fatal"))
        mock_monitor.stop = AsyncMock()

        with (
            patch("kis_websocket_monitor.init_sentry") as mock_init_sentry,
            patch("kis_websocket_monitor.capture_exception") as mock_capture_exception,
            patch(
                "kis_websocket_monitor.KISWebSocketMonitor", return_value=mock_monitor
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await main()

        assert exc_info.value.code == 1
        mock_init_sentry.assert_called_once_with(service_name="auto-trader-kis-ws")
        mock_capture_exception.assert_called_once()
        mock_monitor.stop.assert_awaited_once()


@pytest.mark.unit
class TestKISWebSocketMonitorAccountMode:
    @pytest.mark.asyncio
    async def test_initialize_websocket_passes_kis_mock_account_mode(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "kis_websocket_monitor.settings.kis_ws_is_mock", True, raising=False
        )
        captured: dict = {}

        class _StubWS:
            def __init__(self, on_execution, mock_mode, *, account_mode=None):
                captured["mock_mode"] = mock_mode
                captured["account_mode"] = account_mode

        monkeypatch.setattr("kis_websocket_monitor.KISExecutionWebSocket", _StubWS)

        monitor = KISWebSocketMonitor()
        with caplog.at_level("INFO"):
            await monitor._initialize_websocket()

        assert captured["mock_mode"] is True
        assert captured["account_mode"] == "kis_mock"
        assert any(
            "account_mode=kis_mock" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_initialize_websocket_passes_kis_live_account_mode(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "kis_websocket_monitor.settings.kis_ws_is_mock", False, raising=False
        )
        captured: dict = {}

        class _StubWS:
            def __init__(self, on_execution, mock_mode, *, account_mode=None):
                captured["mock_mode"] = mock_mode
                captured["account_mode"] = account_mode

        monkeypatch.setattr("kis_websocket_monitor.KISExecutionWebSocket", _StubWS)

        monitor = KISWebSocketMonitor()
        with caplog.at_level("INFO"):
            await monitor._initialize_websocket()

        assert captured["mock_mode"] is False
        assert captured["account_mode"] == "kis_live"
        assert any(
            "account_mode=kis_live" in record.message for record in caplog.records
        )
