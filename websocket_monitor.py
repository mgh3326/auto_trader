#!/usr/bin/env python3
"""
Unified WebSocket Monitor

Upbit/KIS 체결 WebSocket을 통합하여 OpenClaw Gateway로 체결 알림을 전송합니다.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.core.config import settings
from app.monitoring.sentry import capture_exception, init_sentry
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.fill_notification import (
    FillOrder,
    normalize_kis_fill,
    normalize_upbit_fill,
)
from app.services.kis_websocket import KISExecutionWebSocket
from app.services.openclaw_client import OpenClawClient
from app.services.upbit_websocket import UpbitMyOrderWebSocket

logger = logging.getLogger(__name__)
VALID_MONITOR_MODES = {"upbit", "kis", "both"}
MIN_FILL_NOTIFY_AMOUNT = 50_000

# Default heartbeat configuration
DEFAULT_HEARTBEAT_PATH = "/tmp/websocket_monitor_heartbeat.json"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
DEFAULT_HEALTH_LOG_INTERVAL_SECONDS = 300.0
DEFAULT_RECONNECT_DELAY_SECONDS = 5.0


class UnifiedWebSocketMonitor:
    """
    통합 WebSocket 모니터

    Upbit/KIS 체결 이벤트를 수신하여 OpenClaw로 체결 알림을 전송합니다.
    """

    def __init__(self, mode: str = "both"):
        if mode not in VALID_MONITOR_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Expected one of: {sorted(VALID_MONITOR_MODES)}"
            )

        self.mode = mode
        self.is_running = False
        self.openclaw_client = OpenClawClient()
        self.upbit_ws: UpbitMyOrderWebSocket | None = None
        self.kis_ws: KISExecutionWebSocket | None = None
        self._health_log_interval_seconds = float(
            os.environ.get(
                "WS_MONITOR_HEALTH_LOG_INTERVAL_SECONDS",
                str(DEFAULT_HEALTH_LOG_INTERVAL_SECONDS),
            )
        )
        self._next_health_log_at = 0.0
        self._started_at_monotonic: float | None = None
        self.fills_forwarded = 0
        self.last_openclaw_success_at: str | None = None
        self._kis_messages_received_closed = 0
        self._kis_execution_events_received_closed = 0
        self._kis_last_message_at_closed: str | None = None
        self._kis_last_execution_at_closed: str | None = None
        self._kis_last_pingpong_at_closed: str | None = None
        self._setup_signal_handlers()

        # Heartbeat configuration from environment
        self._heartbeat_path = os.environ.get(
            "WS_MONITOR_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH
        )
        self._heartbeat_interval_seconds = float(
            os.environ.get(
                "WS_MONITOR_HEARTBEAT_INTERVAL_SECONDS",
                str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
            )
        )
        self._reconnect_delay_seconds = float(
            os.environ.get(
                "WS_MONITOR_RECONNECT_DELAY_SECONDS",
                str(DEFAULT_RECONNECT_DELAY_SECONDS),
            )
        )
        self._last_heartbeat_at = 0.0

    def _setup_signal_handlers(self):
        """SIGINT/SIGTERM 시그널 핸들러 설정"""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        logger.info("Signal handlers installed (SIGINT, SIGTERM)")

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """시그널 수신 시 graceful shutdown"""
        sig_name = signal.Signals(signum).name
        logger.info(f"Received signal {sig_name} ({signum}), initiating shutdown...")
        self.is_running = False

    def _write_heartbeat(self, is_running: bool | None = None) -> None:
        """
        Write heartbeat file atomically.

        Args:
            is_running: Override for is_running status. If None, uses self.is_running.
        """
        import time

        if is_running is None:
            is_running = self.is_running

        upbit_enabled = self.mode in {"upbit", "both"}
        kis_enabled = self.mode in {"kis", "both"}

        upbit_connected: bool | str = (
            bool(self.upbit_ws and self.upbit_ws.is_connected)
            if upbit_enabled
            else "n/a"
        )
        kis_connected: bool | str = (
            bool(self.kis_ws and self.kis_ws.is_connected) if kis_enabled else "n/a"
        )

        data = {
            "updated_at_unix": time.time(),
            "mode": self.mode,
            "is_running": is_running,
            "upbit_connected": upbit_connected,
            "kis_connected": kis_connected,
        }

        # Atomic write: write to temp file, then rename
        heartbeat_path = Path(self._heartbeat_path)
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = heartbeat_path.with_suffix(".tmp")

        try:
            with open(temp_path, "w") as f:
                json.dump(data, f)
            temp_path.replace(heartbeat_path)
        except OSError as e:
            logger.warning("Failed to write heartbeat file: %s", e)

    async def _on_upbit_order(self, order_data: dict[str, Any]) -> None:
        """
        Upbit 주문/체결 이벤트 처리

        state == "trade"인 체결만 알림 대상으로 필터링합니다.
        """
        state = order_data.get("state")
        if state != "trade":
            logger.debug(f"Upbit non-trade state ignored: {state}")
            return

        try:
            fill_order = normalize_upbit_fill(order_data)
            await self._send_fill_notification(fill_order)
            logger.info(
                f"Upbit fill processed: {fill_order.symbol} {fill_order.side} "
                f"{fill_order.filled_qty}@{fill_order.filled_price}"
            )
        except Exception as e:
            logger.error(f"Upbit fill processing error: {e}", exc_info=True)

    async def _on_kis_execution(self, event: dict[str, Any]) -> None:
        """
        KIS 체결 이벤트 처리

        파서 결과를 베스트에포트 정규화하여 알림 전송합니다.
        """
        try:
            if not self._is_valid_kis_fill_event(event):
                return
            fill_order = normalize_kis_fill(event)
            correlation_id = str(event.get("correlation_id") or "n/a")
            logger.info(
                "KIS fill detected: correlation_id=%s symbol=%s side=%s filled_qty=%s filled_price=%s",
                correlation_id,
                fill_order.symbol,
                fill_order.side,
                fill_order.filled_qty,
                fill_order.filled_price,
            )
            await self._send_fill_notification(
                fill_order, correlation_id=correlation_id
            )
            logger.info(
                f"KIS fill processed: {fill_order.symbol} {fill_order.side} "
                f"{fill_order.filled_qty}@{fill_order.filled_price}"
            )
        except Exception as e:
            logger.error(f"KIS fill processing error: {e}", exc_info=True)

    def _is_valid_kis_fill_event(self, event: dict[str, Any]) -> bool:
        market = str(event.get("market", "")).strip().lower()
        if market != "kr":
            return True

        fill_yn = str(event.get("fill_yn") or event.get("cntg_yn") or "").strip()
        if fill_yn != "2":
            logger.debug(
                "Skip KIS domestic notification due to fill_yn: symbol=%s fill_yn=%s",
                event.get("symbol"),
                fill_yn or "<missing>",
            )
            return False

        filled_price = self._to_float(event.get("filled_price"))
        filled_qty = self._to_float(event.get("filled_qty"))
        if filled_price <= 0 or filled_qty <= 0:
            logger.error(
                "Invalid KIS domestic fill values: symbol=%s market=%s filled_price=%s filled_qty=%s fill_yn=%s",
                event.get("symbol"),
                event.get("market"),
                event.get("filled_price"),
                event.get("filled_qty"),
                fill_yn,
            )
            return False
        return True

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    async def _send_fill_notification(
        self, order: FillOrder, *, correlation_id: str | None = None
    ) -> None:
        """OpenClaw로 체결 알림 전송 (fire-and-forget)"""
        if not settings.OPENCLAW_ENABLED:
            logger.info(
                "OpenClaw send result: correlation_id=%s symbol=%s account=%s "
                "result=skipped reason=openclaw_disabled",
                correlation_id,
                order.symbol,
                order.account,
            )
            return

        if order.account == "upbit" and order.filled_amount < MIN_FILL_NOTIFY_AMOUNT:
            logger.debug(
                "Fill below minimum notify amount (%s < %s), skipping: %s",
                f"{order.filled_amount:,.0f}",
                f"{MIN_FILL_NOTIFY_AMOUNT:,.0f}",
                order.symbol,
            )
            logger.info(
                "OpenClaw send result: correlation_id=%s symbol=%s account=%s "
                "result=skipped reason=below_minimum_notify_amount filled_amount=%s minimum_amount=%s",
                correlation_id,
                order.symbol,
                order.account,
                f"{order.filled_amount:,.0f}",
                f"{MIN_FILL_NOTIFY_AMOUNT:,.0f}",
            )
            return

        logger.info(
            "OpenClaw send start: correlation_id=%s symbol=%s account=%s filled_amount=%s",
            correlation_id,
            order.symbol,
            order.account,
            order.filled_amount,
        )
        try:
            request_id = await self.openclaw_client.send_fill_notification(
                order, correlation_id=correlation_id
            )
            if request_id:
                self.fills_forwarded += 1
                self.last_openclaw_success_at = datetime.now(UTC).isoformat()
                logger.info(
                    "OpenClaw send result: correlation_id=%s symbol=%s account=%s "
                    "result=success request_id=%s",
                    correlation_id,
                    order.symbol,
                    order.account,
                    request_id,
                )
            else:
                logger.warning(
                    "OpenClaw send result: correlation_id=%s symbol=%s account=%s "
                    "result=failed request_id=<none>",
                    correlation_id,
                    order.symbol,
                    order.account,
                )
        except Exception as e:
            logger.error(
                "OpenClaw send result: correlation_id=%s symbol=%s account=%s "
                "result=failed error=%s",
                correlation_id,
                order.symbol,
                order.account,
                e,
                exc_info=True,
            )

    async def _start_upbit_supervisor(self) -> None:
        """
        Upbit WebSocket supervisor loop with auto-reconnect.

        When connection closes and is_running=True, reconnects after delay.
        Only exits when is_running=False (stop signal).
        """
        while self.is_running:
            try:
                self.upbit_ws = UpbitMyOrderWebSocket(
                    on_order_callback=self._on_upbit_order,
                    verify_ssl=False,
                )
                logger.info("Connecting to Upbit WebSocket...")
                await self.upbit_ws.connect_and_subscribe()
                if self.upbit_ws.is_connected is not True:
                    raise RuntimeError("Upbit WebSocket connection not established")
                logger.info("Upbit WebSocket connected")

                # Connection closed normally - check if we should reconnect
                if self.is_running:
                    logger.warning(
                        "Upbit WebSocket connection closed, reconnecting in %.1fs...",
                        self._reconnect_delay_seconds,
                    )
                    await asyncio.sleep(self._reconnect_delay_seconds)
                else:
                    logger.info("Upbit WebSocket exiting (stop signal)")
                    break
            except Exception as e:
                logger.error("Upbit WebSocket error: %s", e, exc_info=True)
                if self.is_running:
                    logger.info(
                        "Reconnecting Upbit in %.1fs...", self._reconnect_delay_seconds
                    )
                    await asyncio.sleep(self._reconnect_delay_seconds)
                else:
                    raise

    async def _start_kis_supervisor(self) -> None:
        """
        KIS WebSocket supervisor loop with auto-reconnect.

        When connection closes and is_running=True, reconnects after delay.
        Only exits when is_running=False (stop signal).
        """
        while self.is_running:
            try:
                self.kis_ws = KISExecutionWebSocket(
                    on_execution=cast(
                        Callable[[dict[str, Any]], None],
                        self._on_kis_execution,
                    ),
                    mock_mode=settings.kis_ws_is_mock,
                )
                self.kis_ws.is_running = True
                logger.info("Connecting to KIS WebSocket...")
                await self.kis_ws.connect_and_subscribe()
                await self.kis_ws.listen()

                # listen() returned - connection closed
                if self.is_running:
                    self._fold_kis_socket_stats(self.kis_ws)
                    self.kis_ws = None
                    logger.warning(
                        "KIS WebSocket connection closed, reconnecting in %.1fs...",
                        self._reconnect_delay_seconds,
                    )
                    await asyncio.sleep(self._reconnect_delay_seconds)
                else:
                    logger.info("KIS WebSocket exiting (stop signal)")
                    break
            except Exception as e:
                logger.error("KIS WebSocket error: %s", e, exc_info=True)
                if self.is_running:
                    self._fold_kis_socket_stats(self.kis_ws)
                    self.kis_ws = None
                    logger.info(
                        "Reconnecting KIS in %.1fs...", self._reconnect_delay_seconds
                    )
                    await asyncio.sleep(self._reconnect_delay_seconds)
                else:
                    raise

    async def _start_upbit(self) -> None:
        """Upbit WebSocket 시작 (supervisor wrapper)."""
        await self._start_upbit_supervisor()

    async def _start_kis(self) -> None:
        """KIS WebSocket 시작 (supervisor wrapper)."""
        await self._start_kis_supervisor()

    def _log_health_status(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now < self._next_health_log_at:
            return

        self._next_health_log_at = now + self._health_log_interval_seconds

        upbit_enabled = self.mode in {"upbit", "both"}
        kis_enabled = self.mode in {"kis", "both"}
        upbit_connected: bool | str = (
            bool(self.upbit_ws and self.upbit_ws.is_connected)
            if upbit_enabled
            else "n/a"
        )
        kis_connected: bool | str = (
            bool(self.kis_ws and self.kis_ws.is_connected) if kis_enabled else "n/a"
        )
        enabled_states: list[bool] = []
        if upbit_enabled:
            enabled_states.append(bool(upbit_connected))
        if kis_enabled:
            enabled_states.append(bool(kis_connected))
        connected = all(enabled_states) if enabled_states else False
        uptime = 0.0
        if self._started_at_monotonic is not None:
            uptime = round(now - self._started_at_monotonic, 1)
        kis_snapshot = self._current_kis_stats_snapshot()
        logger.info(
            "Unified WebSocket health: mode=%s connected=%s uptime=%s "
            "upbit_connected=%s kis_connected=%s "
            "messages_received=%s execution_events_received=%s fills_forwarded=%s "
            "last_message_at=%s last_execution_at=%s last_pingpong_at=%s "
            "last_openclaw_success_at=%s",
            self.mode,
            connected,
            uptime,
            upbit_connected,
            kis_connected,
            kis_snapshot["messages_received"],
            kis_snapshot["execution_events_received"],
            self.fills_forwarded,
            kis_snapshot["last_message_at"],
            kis_snapshot["last_execution_at"],
            kis_snapshot["last_pingpong_at"],
            self.last_openclaw_success_at,
        )

    async def start(self) -> None:
        """통합 모니터링 시작"""
        logger.info("Starting Unified WebSocket Monitor (mode=%s)...", self.mode)
        self.is_running = True
        self._started_at_monotonic = asyncio.get_running_loop().time()

        # Write initial heartbeat
        self._write_heartbeat()

        task_map: dict[str, asyncio.Task[Any]] = {}
        if self.mode in {"upbit", "both"}:
            task_map["upbit"] = asyncio.create_task(
                self._start_upbit(), name="upbit-websocket"
            )
        if self.mode in {"kis", "both"}:
            task_map["kis"] = asyncio.create_task(
                self._start_kis(), name="kis-websocket"
            )

        if not task_map:
            raise RuntimeError(f"No websocket task selected for mode={self.mode}")

        failure: RuntimeError | None = None

        try:
            while self.is_running:
                done, _ = await asyncio.wait(
                    set(task_map.values()),
                    timeout=self._heartbeat_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Update heartbeat periodically
                self._write_heartbeat()

                if not done:
                    self._log_health_status()
                    continue

                for name, task in task_map.items():
                    if task not in done:
                        continue

                    if task.cancelled():
                        failure = RuntimeError(
                            f"{name} task was cancelled unexpectedly"
                        )
                        logger.error("%s task cancelled unexpectedly", name)
                    else:
                        exc = task.exception()
                        if exc:
                            failure = RuntimeError(f"{name} task failed: {exc}")
                            logger.error("%s task failed: %s", name, exc, exc_info=exc)
                        else:
                            # Task completed normally - supervisor exited
                            # This means stop was requested, which is fine
                            if self.is_running:
                                failure = RuntimeError(
                                    f"{name} supervisor exited unexpectedly"
                                )
                                logger.error("%s supervisor exited unexpectedly", name)

                    if failure:
                        self.is_running = False
                        break
        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
            raise
        finally:
            for task in task_map.values():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.debug("Child task cleanup ignored error: %s", exc)

            self._log_health_status(force=True)
            self._write_heartbeat(is_running=False)

        if failure is not None:
            raise failure

    async def stop(self) -> None:
        """통합 모니터링 정지"""
        logger.info("Stopping Unified WebSocket Monitor...")
        self.is_running = False

        # Write heartbeat to indicate stopped
        self._write_heartbeat(is_running=False)

        if self.upbit_ws:
            try:
                await self.upbit_ws.disconnect()
            except Exception as e:
                logger.warning(f"Failed to stop Upbit WebSocket cleanly: {e}")

        if self.kis_ws:
            try:
                self._fold_kis_socket_stats(self.kis_ws)
                await self.kis_ws.stop()
            except Exception as e:
                logger.warning(f"Failed to stop KIS WebSocket cleanly: {e}")

        logger.info("Unified WebSocket Monitor stopped")

    @staticmethod
    def _latest_timestamp(*timestamps: str | None) -> str | None:
        values = [timestamp for timestamp in timestamps if timestamp]
        return max(values) if values else None

    @staticmethod
    def _read_kis_socket_stats(
        websocket: KISExecutionWebSocket | Any | None,
    ) -> dict[str, int | str | None]:
        if websocket is None:
            return {
                "messages_received": 0,
                "execution_events_received": 0,
                "last_message_at": None,
                "last_execution_at": None,
                "last_pingpong_at": None,
            }
        snapshot_reader = getattr(websocket, "get_runtime_snapshot", None)
        if callable(snapshot_reader):
            snapshot = snapshot_reader()
            if isinstance(snapshot, dict):
                return snapshot
        return {
            "messages_received": int(getattr(websocket, "messages_received", 0) or 0),
            "execution_events_received": int(
                getattr(websocket, "execution_events_received", 0) or 0
            ),
            "last_message_at": getattr(websocket, "last_message_at", None),
            "last_execution_at": getattr(websocket, "last_execution_at", None),
            "last_pingpong_at": getattr(websocket, "last_pingpong_at", None),
        }

    def _fold_kis_socket_stats(
        self, websocket: KISExecutionWebSocket | Any | None
    ) -> None:
        snapshot = self._read_kis_socket_stats(websocket)
        self._kis_messages_received_closed += int(snapshot["messages_received"] or 0)
        self._kis_execution_events_received_closed += int(
            snapshot["execution_events_received"] or 0
        )
        self._kis_last_message_at_closed = self._latest_timestamp(
            self._kis_last_message_at_closed,
            snapshot["last_message_at"]
            if isinstance(snapshot["last_message_at"], str)
            else None,
        )
        self._kis_last_execution_at_closed = self._latest_timestamp(
            self._kis_last_execution_at_closed,
            snapshot["last_execution_at"]
            if isinstance(snapshot["last_execution_at"], str)
            else None,
        )
        self._kis_last_pingpong_at_closed = self._latest_timestamp(
            self._kis_last_pingpong_at_closed,
            snapshot["last_pingpong_at"]
            if isinstance(snapshot["last_pingpong_at"], str)
            else None,
        )

    def _current_kis_stats_snapshot(self) -> dict[str, int | str | None]:
        current_snapshot = self._read_kis_socket_stats(self.kis_ws)
        return {
            "messages_received": self._kis_messages_received_closed
            + int(current_snapshot["messages_received"] or 0),
            "execution_events_received": self._kis_execution_events_received_closed
            + int(current_snapshot["execution_events_received"] or 0),
            "last_message_at": self._latest_timestamp(
                self._kis_last_message_at_closed,
                current_snapshot["last_message_at"]
                if isinstance(current_snapshot["last_message_at"], str)
                else None,
            ),
            "last_execution_at": self._latest_timestamp(
                self._kis_last_execution_at_closed,
                current_snapshot["last_execution_at"]
                if isinstance(current_snapshot["last_execution_at"], str)
                else None,
            ),
            "last_pingpong_at": self._latest_timestamp(
                self._kis_last_pingpong_at_closed,
                current_snapshot["last_pingpong_at"]
                if isinstance(current_snapshot["last_pingpong_at"], str)
                else None,
            ),
        }


async def main(mode: str = "both") -> None:
    """메인 엔트리포인트"""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    service_name = {
        "upbit": "auto-trader-upbit-ws",
        "kis": "auto-trader-kis-ws",
        "both": "auto-trader-websocket",
    }[mode]
    init_sentry(service_name=service_name)

    has_telegram = bool(settings.telegram_token and settings.telegram_chat_id)
    has_discord = any(
        [
            settings.discord_webhook_us,
            settings.discord_webhook_kr,
            settings.discord_webhook_crypto,
            settings.discord_webhook_alerts,
        ]
    )

    if has_telegram or has_discord:
        try:
            trade_notifier = get_trade_notifier()
            trade_notifier.configure(
                bot_token=settings.telegram_token or "",
                chat_ids=settings.telegram_chat_ids
                if settings.telegram_chat_ids
                else [],
                enabled=True,
                discord_webhook_us=settings.discord_webhook_us,
                discord_webhook_kr=settings.discord_webhook_kr,
                discord_webhook_crypto=settings.discord_webhook_crypto,
                discord_webhook_alerts=settings.discord_webhook_alerts,
            )
            logger.info(
                "Trade notifier configured: telegram=%s discord=%s",
                has_telegram,
                has_discord,
            )
        except Exception as e:
            logger.warning("Failed to configure trade notifier: %s", e, exc_info=True)
    else:
        logger.info("Trade notifier disabled: no Telegram or Discord configured")

    monitor = UnifiedWebSocketMonitor(mode=mode)

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        capture_exception(e, process="websocket_monitor")
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await monitor.stop()
        try:
            trade_notifier = get_trade_notifier()
            await trade_notifier.shutdown()
        except Exception as e:
            logger.warning("Failed to shutdown trade notifier: %s", e, exc_info=True)
        logger.info("Unified WebSocket Monitor exited gracefully")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified websocket monitor")
    parser.add_argument(
        "--mode",
        choices=sorted(VALID_MONITOR_MODES),
        default="both",
        help="Run only selected websocket backend",
    )
    args = parser.parse_args()
    asyncio.run(main(mode=args.mode))
