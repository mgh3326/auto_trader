"""Tests for unified WebSocket monitor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.services.fill_notification import FillOrder


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")
    monkeypatch.setattr(settings, "kis_ws_is_mock", True)


class TestUnifiedWebSocketMonitor:
    """통합 WebSocket 모니터 테스트"""

    @pytest.mark.asyncio
    async def test_on_upbit_trade_sends_notification(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-123")
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._on_upbit_order(
            {
                "code": "KRW-BTC",
                "ask_bid": "BID",
                "trade_price": 50_000_000,
                "trade_volume": 0.1,
                "state": "trade",
                "trade_timestamp": 1_700_000_000_000,
            }
        )

        send_mock.assert_awaited_once()
        fill_order = send_mock.call_args.args[0]
        assert isinstance(fill_order, FillOrder)
        assert fill_order.symbol == "KRW-BTC"
        assert fill_order.side == "bid"

    @pytest.mark.asyncio
    async def test_on_upbit_non_trade_ignored(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-123")
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._on_upbit_order(
            {
                "code": "KRW-BTC",
                "ask_bid": "BID",
                "trade_price": 50_000_000,
                "trade_volume": 0.1,
                "state": "wait",
            }
        )

        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_kis_execution_sends_notification(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-456")
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._on_kis_execution(
            {
                "symbol": "005930",
                "side": "sell",
                "fill_yn": "2",
                "filled_price": 70_000,
                "filled_qty": 10,
                "market": "kr",
                "correlation_id": "corr-kis-1",
            }
        )

        send_mock.assert_awaited_once()
        fill_order = send_mock.call_args.args[0]
        assert isinstance(fill_order, FillOrder)
        assert fill_order.symbol == "005930"
        assert fill_order.side == "ask"
        assert send_mock.await_args is not None
        assert send_mock.await_args.kwargs["correlation_id"] == "corr-kis-1"

    @pytest.mark.asyncio
    async def test_on_kis_execution_skips_domestic_without_fill_yn(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-456")
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._on_kis_execution(
            {
                "symbol": "035420",
                "side": "bid",
                "filled_price": 2,
                "filled_qty": 1,
                "market": "kr",
            }
        )

        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_kis_execution_skips_domestic_non_fill_event(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-456")
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._on_kis_execution(
            {
                "symbol": "035420",
                "side": "bid",
                "fill_yn": "1",
                "filled_price": 1_135_000,
                "filled_qty": 2,
                "market": "kr",
            }
        )

        send_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_stops_when_child_task_fails(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="both")

        async def slow_upbit() -> None:
            await asyncio.sleep(60)

        async def fail_kis() -> None:
            raise RuntimeError("boom")

        monitor._start_upbit = slow_upbit  # type: ignore[method-assign]
        monitor._start_kis = fail_kis  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="kis task failed"):
            await monitor.start()

        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_start_mode_upbit_does_not_start_kis(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")

        async def fail_upbit() -> None:
            raise RuntimeError("upbit boom")

        never_called_kis = AsyncMock()

        monitor._start_upbit = fail_upbit  # type: ignore[method-assign]
        monitor._start_kis = never_called_kis  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="upbit task failed"):
            await monitor.start()

        never_called_kis.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_mode_kis_does_not_start_upbit(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="kis")

        async def fail_kis() -> None:
            raise RuntimeError("kis boom")

        never_called_upbit = AsyncMock()

        monitor._start_kis = fail_kis  # type: ignore[method-assign]
        monitor._start_upbit = never_called_upbit  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="kis task failed"):
            await monitor.start()

        never_called_upbit.assert_not_awaited()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_reraises_cancelled_error_after_cleanup(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        started = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            await asyncio.Future()

        monitor._start_upbit = wait_forever  # type: ignore[method-assign]

        task = asyncio.create_task(monitor.start())
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    def test_invalid_mode_raises_value_error(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        with pytest.raises(ValueError, match="Invalid mode"):
            UnifiedWebSocketMonitor(mode="invalid")

    @pytest.mark.asyncio
    async def test_stop_cleans_up_resources(self, mock_settings: None) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        upbit_disconnect = AsyncMock()
        kis_stop = AsyncMock()

        monitor.upbit_ws = MagicMock()
        monitor.upbit_ws.disconnect = upbit_disconnect

        monitor.kis_ws = MagicMock()
        monitor.kis_ws.stop = kis_stop

        await monitor.stop()

        upbit_disconnect.assert_awaited_once()
        kis_stop.assert_awaited_once()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_send_fill_notification_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "OPENCLAW_ENABLED", False)
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        monitor.openclaw_client.send_fill_notification = AsyncMock(
            return_value="req-123"
        )

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=50_000_000,
                filled_qty=0.1,
                filled_amount=5_000_000,
                filled_at="2024-01-01T00:00:00Z",
                account="upbit",
            )
        )

        monitor.openclaw_client.send_fill_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_fill_notification_skips_upbit_below_minimum(
        self, mock_settings: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from websocket_monitor import MIN_FILL_NOTIFY_AMOUNT, UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-123")
        monitor.openclaw_client.send_fill_notification = send_mock

        caplog.set_level("DEBUG")

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=49_999,
                filled_qty=1,
                filled_amount=MIN_FILL_NOTIFY_AMOUNT - 1,
                filled_at="2024-01-01T00:00:00Z",
                account="upbit",
            )
        )

        send_mock.assert_not_awaited()
        assert "Fill below minimum notify amount" in caplog.text

    @pytest.mark.asyncio
    async def test_send_fill_notification_does_not_filter_kis_low_amount(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value="req-123")
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._send_fill_notification(
            FillOrder(
                symbol="AAPL",
                side="bid",
                filled_price=35,
                filled_qty=1,
                filled_amount=35,
                filled_at="2024-01-01T00:00:00Z",
                account="kis",
            )
        )

        send_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_fill_notification_continues_on_failure(
        self, mock_settings: None
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor()
        send_mock = AsyncMock(return_value=None)
        monitor.openclaw_client.send_fill_notification = send_mock

        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=50_000_000,
                filled_qty=0.1,
                filled_amount=5_000_000,
                filled_at="2024-01-01T00:00:00Z",
                account="upbit",
            )
        )

        send_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_fill_notification_logs_openclaw_result_states(
        self, mock_settings: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="kis")
        order = FillOrder(
            symbol="005930",
            side="ask",
            filled_price=70_000,
            filled_qty=10,
            filled_amount=700_000,
            filled_at="2024-01-01T00:00:00Z",
            account="kis",
        )

        caplog.set_level("INFO")

        monitor.openclaw_client.send_fill_notification = AsyncMock(
            return_value="req-123"
        )
        await monitor._send_fill_notification(order, correlation_id="corr-success")
        assert monitor.fills_forwarded == 1
        assert monitor.last_openclaw_success_at is not None
        assert "correlation_id=corr-success" in caplog.text
        assert "OpenClaw send start" in caplog.text
        assert "OpenClaw send result" in caplog.text
        assert "result=success" in caplog.text
        assert "Notification pipeline result" not in caplog.text

        caplog.clear()
        monitor.openclaw_client.send_fill_notification = AsyncMock(return_value=None)
        await monitor._send_fill_notification(order, correlation_id="corr-failed")
        assert "correlation_id=corr-failed" in caplog.text
        assert "OpenClaw send result" in caplog.text
        assert "result=failed" in caplog.text

        caplog.clear()
        monitor.openclaw_client.send_fill_notification = AsyncMock(
            return_value="req-skip"
        )
        monitor.mode = "upbit"
        await monitor._send_fill_notification(
            FillOrder(
                symbol="KRW-BTC",
                side="bid",
                filled_price=10_000,
                filled_qty=1,
                filled_amount=10_000,
                filled_at="2024-01-01T00:00:00Z",
                account="upbit",
            ),
            correlation_id="corr-skipped",
        )
        assert "correlation_id=corr-skipped" in caplog.text
        assert "OpenClaw send result" in caplog.text
        assert "result=skipped" in caplog.text

    @pytest.mark.asyncio
    async def test_log_health_status_uses_kis_state_fields(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setenv("WS_MONITOR_HEALTH_LOG_INTERVAL_SECONDS", "123")
        monitor = UnifiedWebSocketMonitor(mode="kis")
        monitor.is_running = True
        monitor._started_at_monotonic = asyncio.get_running_loop().time() - 42
        monitor.fills_forwarded = 3
        monitor.last_openclaw_success_at = "2026-03-09T14:00:00+00:00"
        monitor.kis_ws = MagicMock(
            is_connected=True,
            messages_received=11,
            execution_events_received=4,
            last_message_at="2026-03-09T14:01:00+00:00",
            last_execution_at="2026-03-09T14:01:05+00:00",
            last_pingpong_at="2026-03-09T14:01:10+00:00",
        )

        assert monitor._health_log_interval_seconds == 123.0

        caplog.set_level("INFO")
        monitor._log_health_status(force=True)

        assert "Unified WebSocket health" in caplog.text
        assert "connected=True" in caplog.text
        assert "messages_received=11" in caplog.text
        assert "execution_events_received=4" in caplog.text
        assert "fills_forwarded=3" in caplog.text
        assert "last_pingpong_at=2026-03-09T14:01:10+00:00" in caplog.text
        assert "last_openclaw_success_at=2026-03-09T14:00:00+00:00" in caplog.text

    @pytest.mark.asyncio
    async def test_log_health_status_reports_mixed_backend_states_in_both_mode(
        self, mock_settings: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="both")
        monitor.is_running = True
        monitor._started_at_monotonic = asyncio.get_running_loop().time() - 10
        monitor.upbit_ws = MagicMock(is_connected=True)
        monitor.kis_ws = MagicMock(
            is_connected=False,
            messages_received=5,
            execution_events_received=2,
            last_message_at="2026-03-09T14:02:00+00:00",
            last_execution_at="2026-03-09T14:02:05+00:00",
            last_pingpong_at="2026-03-09T14:02:06+00:00",
        )

        caplog.set_level("INFO")
        monitor._log_health_status(force=True)

        assert "mode=both" in caplog.text
        assert "connected=False" in caplog.text
        assert "upbit_connected=True" in caplog.text
        assert "kis_connected=False" in caplog.text

    @pytest.mark.asyncio
    async def test_log_health_status_accumulates_closed_kis_sessions(
        self, mock_settings: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="kis")
        monitor.is_running = True
        monitor._started_at_monotonic = asyncio.get_running_loop().time() - 20

        closed_socket = MagicMock(
            is_connected=False,
            get_runtime_snapshot=MagicMock(
                return_value={
                    "messages_received": 7,
                    "execution_events_received": 3,
                    "last_message_at": "2026-03-09T14:03:00+00:00",
                    "last_execution_at": "2026-03-09T14:03:05+00:00",
                    "last_pingpong_at": "2026-03-09T14:03:06+00:00",
                }
            ),
        )
        monitor._fold_kis_socket_stats(closed_socket)

        monitor.kis_ws = MagicMock(
            is_connected=True,
            get_runtime_snapshot=MagicMock(
                return_value={
                    "messages_received": 5,
                    "execution_events_received": 2,
                    "last_message_at": "2026-03-09T14:04:00+00:00",
                    "last_execution_at": "2026-03-09T14:04:05+00:00",
                    "last_pingpong_at": "2026-03-09T14:04:06+00:00",
                }
            ),
        )

        caplog.set_level("INFO")
        monitor._log_health_status(force=True)

        assert "messages_received=12" in caplog.text
        assert "execution_events_received=5" in caplog.text
        assert "last_message_at=2026-03-09T14:04:00+00:00" in caplog.text
        assert "last_execution_at=2026-03-09T14:04:05+00:00" in caplog.text
        assert "last_pingpong_at=2026-03-09T14:04:06+00:00" in caplog.text

    @pytest.mark.asyncio
    async def test_log_health_status_throttles_when_not_forced(
        self, mock_settings: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="kis")
        monitor._next_health_log_at = asyncio.get_running_loop().time() + 60

        caplog.set_level("INFO")
        monitor._log_health_status(force=False)

        assert "Unified WebSocket health" not in caplog.text

    @pytest.mark.asyncio
    async def test_main_configures_and_shuts_down_trade_notifier(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import websocket_monitor

        monitor = MagicMock()
        monitor.start = AsyncMock(return_value=None)
        monitor.stop = AsyncMock(return_value=None)

        notifier = MagicMock()
        notifier.configure = MagicMock()
        notifier.shutdown = AsyncMock(return_value=None)

        monkeypatch.setattr(settings, "telegram_token", "telegram-token")
        monkeypatch.setattr(settings, "telegram_chat_id", "123456")
        # Set Discord webhooks to None/empty to test Telegram-only config
        monkeypatch.setattr(settings, "discord_webhook_us", None)
        monkeypatch.setattr(settings, "discord_webhook_kr", None)
        monkeypatch.setattr(settings, "discord_webhook_crypto", None)
        monkeypatch.setattr(settings, "discord_webhook_alerts", None)
        monkeypatch.setattr(websocket_monitor, "init_sentry", lambda **_: None)
        monkeypatch.setattr(
            websocket_monitor,
            "UnifiedWebSocketMonitor",
            lambda mode="both": monitor,
        )
        monkeypatch.setattr(
            websocket_monitor,
            "get_trade_notifier",
            lambda: notifier,
            raising=False,
        )

        await websocket_monitor.main(mode="both")

        notifier.configure.assert_called_once_with(
            bot_token="telegram-token",
            chat_ids=["123456"],
            enabled=True,
            discord_webhook_us=None,
            discord_webhook_kr=None,
            discord_webhook_crypto=None,
            discord_webhook_alerts=None,
        )
        notifier.shutdown.assert_awaited_once()
        monitor.start.assert_awaited_once()
        monitor.stop.assert_awaited_once()


class TestHeartbeat:
    """Tests for heartbeat file writing."""

    def test_write_heartbeat_creates_file(self, mock_settings: None, tmp_path) -> None:
        """Test that _write_heartbeat creates heartbeat file."""
        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        assert heartbeat_file.exists()

    def test_write_heartbeat_content(self, mock_settings: None, tmp_path) -> None:
        """Test that _write_heartbeat writes correct content."""
        import json
        import time

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor(mode="both")
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        with open(heartbeat_file) as f:
            data = json.load(f)

        assert "updated_at_unix" in data
        assert data["mode"] == "both"
        assert data["is_running"] is False  # Default
        assert data["upbit_connected"] is False
        assert data["kis_connected"] is False
        # Verify timestamp is recent
        assert time.time() - data["updated_at_unix"] < 2

    def test_write_heartbeat_with_override(self, mock_settings: None, tmp_path) -> None:
        """Test that _write_heartbeat respects is_running override."""
        import json

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat(is_running=True)

        with open(heartbeat_file) as f:
            data = json.load(f)

        assert data["is_running"] is True

    def test_write_heartbeat_mode_upbit(self, mock_settings: None, tmp_path) -> None:
        """Test heartbeat shows correct connection status for upbit mode."""
        import json

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        with open(heartbeat_file) as f:
            data = json.load(f)

        assert data["mode"] == "upbit"
        assert data["upbit_connected"] is False
        assert data["kis_connected"] == "n/a"

    def test_write_heartbeat_creates_parent_dir(
        self, mock_settings: None, tmp_path
    ) -> None:
        """Test that _write_heartbeat creates parent directories."""
        import json

        from websocket_monitor import UnifiedWebSocketMonitor

        heartbeat_file = tmp_path / "nested" / "dir" / "heartbeat.json"
        monitor = UnifiedWebSocketMonitor()
        monitor._heartbeat_path = str(heartbeat_file)
        monitor._write_heartbeat()

        assert heartbeat_file.exists()
        with open(heartbeat_file) as f:
            data = json.load(f)
        assert "updated_at_unix" in data


class TestAutoReconnect:
    """Tests for auto-reconnect supervisor behavior."""

    def test_reconnect_delay_configurable(self, mock_settings: None) -> None:
        """Test that reconnect delay is configurable."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor._reconnect_delay_seconds = 5.0

        assert monitor._reconnect_delay_seconds == 5.0

    def test_heartbeat_path_configurable(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that heartbeat path is configurable via environment."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setenv("WS_MONITOR_HEARTBEAT_PATH", "/custom/heartbeat.json")
        monitor = UnifiedWebSocketMonitor()
        assert monitor._heartbeat_path == "/custom/heartbeat.json"

    def test_heartbeat_interval_configurable(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that heartbeat interval is configurable via environment."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.setenv("WS_MONITOR_HEARTBEAT_INTERVAL_SECONDS", "30")
        monitor = UnifiedWebSocketMonitor()
        assert monitor._heartbeat_interval_seconds == 30.0

    def test_health_log_interval_defaults_to_five_minutes(
        self, mock_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from websocket_monitor import UnifiedWebSocketMonitor

        monkeypatch.delenv("WS_MONITOR_HEALTH_LOG_INTERVAL_SECONDS", raising=False)
        monitor = UnifiedWebSocketMonitor()
        assert monitor._health_log_interval_seconds == 300.0

    @pytest.mark.asyncio
    async def test_supervisor_exits_on_stop_before_start(
        self, mock_settings: None
    ) -> None:
        """Test that supervisor exits immediately when is_running is False."""
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor.is_running = False

        # Should exit immediately without attempting connection
        await monitor._start_upbit_supervisor()
        # No assertion needed - just verify it returns cleanly

    @pytest.mark.asyncio
    async def test_upbit_supervisor_reconnects_when_connection_not_established(
        self,
        mock_settings: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import websocket_monitor
        from websocket_monitor import UnifiedWebSocketMonitor

        monitor = UnifiedWebSocketMonitor(mode="upbit")
        monitor.is_running = True

        class FakeUpbitWs:
            def __init__(self, *args, **kwargs):
                self.is_connected = False
                self.connect_and_subscribe = AsyncMock(return_value=None)

        fake_ws = FakeUpbitWs()

        def fake_factory(*args, **kwargs):
            return fake_ws

        async def stop_after_first_sleep(_: float) -> None:
            monitor.is_running = False

        monkeypatch.setattr(websocket_monitor, "UpbitMyOrderWebSocket", fake_factory)
        monkeypatch.setattr(websocket_monitor.asyncio, "sleep", stop_after_first_sleep)
        caplog.set_level("INFO")

        await monitor._start_upbit_supervisor()

        fake_ws.connect_and_subscribe.assert_awaited_once()
        assert "Upbit WebSocket connected" not in caplog.text
        assert "Reconnecting Upbit" in caplog.text
