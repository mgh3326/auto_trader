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
                "filled_price": 70_000,
                "filled_qty": 10,
                "market": "kr",
            }
        )

        send_mock.assert_awaited_once()
        fill_order = send_mock.call_args.args[0]
        assert isinstance(fill_order, FillOrder)
        assert fill_order.symbol == "005930"
        assert fill_order.side == "ask"

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
