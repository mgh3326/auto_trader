#!/usr/bin/env python3
"""
Unified WebSocket Monitor

Upbit/KIS 체결 WebSocket을 통합하여 OpenClaw Gateway로 체결 알림을 전송합니다.
"""

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from app.core.config import settings
from app.monitoring.sentry import capture_exception, init_sentry
from app.services.fill_notification import (
    FillOrder,
    normalize_kis_fill,
    normalize_upbit_fill,
)
from app.services.kis_websocket import KISExecutionWebSocket
from app.services.openclaw_client import OpenClawClient
from app.services.upbit_websocket import UpbitMyOrderWebSocket
from data.coins_info import upbit_pairs

logger = logging.getLogger(__name__)
VALID_MONITOR_MODES = {"upbit", "kis", "both"}


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
        self._health_log_interval_seconds = 30.0
        self._next_health_log_at = 0.0
        self._setup_signal_handlers()

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

    async def _on_upbit_order(self, order_data: dict) -> None:
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
            fill_order = normalize_kis_fill(event)
            await self._send_fill_notification(fill_order)
            logger.info(
                f"KIS fill processed: {fill_order.symbol} {fill_order.side} "
                f"{fill_order.filled_qty}@{fill_order.filled_price}"
            )
        except Exception as e:
            logger.error(f"KIS fill processing error: {e}", exc_info=True)

    async def _send_fill_notification(self, order: FillOrder) -> None:
        """OpenClaw로 체결 알림 전송 (fire-and-forget)"""
        if not settings.OPENCLAW_ENABLED:
            logger.debug("OpenClaw disabled, skipping fill notification")
            return

        try:
            request_id = await self.openclaw_client.send_fill_notification(order)
            if request_id:
                logger.debug(f"Fill notification sent: request_id={request_id}")
            else:
                logger.warning("Fill notification failed after retries, continuing...")
        except Exception as e:
            logger.error(f"Fill notification error: {e}", exc_info=True)

    async def _start_upbit(self) -> None:
        """Upbit WebSocket 시작"""
        await upbit_pairs.prime_upbit_constants()
        self.upbit_ws = UpbitMyOrderWebSocket(
            on_order_callback=self._on_upbit_order,
            verify_ssl=False,
        )
        logger.info("Starting Upbit WebSocket...")
        await self.upbit_ws.connect_and_subscribe()

        if self.is_running:
            raise RuntimeError("Upbit WebSocket task exited unexpectedly")

    async def _start_kis(self) -> None:
        """KIS WebSocket 시작"""
        self.kis_ws = KISExecutionWebSocket(
            on_execution=self._on_kis_execution,
            mock_mode=settings.kis_ws_is_mock,
        )
        self.kis_ws.is_running = True
        logger.info("Starting KIS WebSocket...")
        await self.kis_ws.connect_and_subscribe()
        await self.kis_ws.listen()

        if self.is_running:
            raise RuntimeError("KIS WebSocket task exited unexpectedly")

    def _log_health_status(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now < self._next_health_log_at:
            return

        self._next_health_log_at = now + self._health_log_interval_seconds

        upbit_enabled = self.mode in {"upbit", "both"}
        kis_enabled = self.mode in {"kis", "both"}
        upbit_connected: bool | str = (
            bool(self.upbit_ws and self.upbit_ws.is_connected) if upbit_enabled else "n/a"
        )
        kis_connected: bool | str = (
            bool(self.kis_ws and self.kis_ws.is_connected) if kis_enabled else "n/a"
        )
        logger.info(
            "Unified WebSocket health: upbit_connected=%s kis_connected=%s openclaw_enabled=%s",
            upbit_connected,
            kis_connected,
            settings.OPENCLAW_ENABLED,
        )

    async def start(self) -> None:
        """통합 모니터링 시작"""
        logger.info("Starting Unified WebSocket Monitor (mode=%s)...", self.mode)
        self.is_running = True

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
                    timeout=5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
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
                            failure = RuntimeError(
                                f"{name} task exited unexpectedly without exception"
                            )
                            logger.error(
                                "%s task exited unexpectedly without exception", name
                            )

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

        if failure is not None:
            raise failure

    async def stop(self) -> None:
        """통합 모니터링 정지"""
        logger.info("Stopping Unified WebSocket Monitor...")
        self.is_running = False

        if self.upbit_ws:
            try:
                await self.upbit_ws.disconnect()
            except Exception as e:
                logger.warning(f"Failed to stop Upbit WebSocket cleanly: {e}")

        if self.kis_ws:
            try:
                await self.kis_ws.stop()
            except Exception as e:
                logger.warning(f"Failed to stop KIS WebSocket cleanly: {e}")

        logger.info("Unified WebSocket Monitor stopped")


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
