#!/usr/bin/env python3
"""
KIS WebSocket Monitor - Main Entry Point

KIS 체결 WebSocket을 모니터링하여 체결 이벤트를 Redis pub/sub으로 발행합니다.

사용법:
    python kis_websocket_monitor.py

종료: Ctrl+C (SIGINT) 또는 SIGTERM 신호
"""

import asyncio
import logging
import signal
import sys
from typing import Any

from app.core.config import settings
from app.monitoring.sentry import capture_exception, init_sentry
from app.services.execution_event import (
    close_redis as close_execution_redis,
)
from app.services.execution_event import (
    publish_execution_event,
)
from app.services.kis_websocket import KISExecutionWebSocket

logger = logging.getLogger(__name__)


class KISWebSocketMonitor:
    """
    KIS WebSocket 모니터

    WebSocket 연결, 체결 처리, 이벤트 발행을 담당합니다.
    """

    def __init__(self):
        self.websocket_client: KISExecutionWebSocket | None = None
        self.is_running = False
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """
        시그널 핸들러 설정

        SIGINT (Ctrl+C) 및 SIGTERM 시 graceful shutdown을 수행합니다.
        """
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        logger.info("Signal handlers installed (SIGINT, SIGTERM)")

    def _handle_signal(self, signum, frame):
        """
        시그널 수신 처리

        Args:
            signum: 시그널 번호
            frame: 현재 스택 프레임
        """
        sig_name = signal.Signals(signum).name
        logger.info(
            f"Received signal {sig_name} ({signum}), initiating graceful shutdown..."
        )
        self.is_running = False

    async def _initialize_websocket(self):
        """
        KIS WebSocket 클라이언트 초기화

        체결 이벤트 콜백을 등록하고 ROB-100 account_mode 를 명시적으로 전달합니다.
        """
        is_mock = bool(settings.kis_ws_is_mock)
        account_mode = "kis_mock" if is_mock else "kis_live"

        logger.info(
            "Initializing KIS WebSocket: account_mode=%s mock_mode=%s "
            "ws_url=%s",
            account_mode,
            is_mock,
            "ws://ops.koreainvestment.com:31000/tryitout"
            if is_mock
            else "ws://ops.koreainvestment.com:21000/tryitout",
        )

        self.websocket_client = KISExecutionWebSocket(
            on_execution=self._on_execution,
            mock_mode=is_mock,
            account_mode=account_mode,
        )
        logger.info("KIS WebSocket client initialized")

    async def _on_execution(self, event: dict[str, Any]):
        """
        체결 이벤트 수신 처리

        체결 이벤트를 수신하면 Redis 이벤트를 발행합니다.

        Args:
            event: 체결 이벤트 데이터
                symbol, side, order_id, filled_price, filled_qty, exec_time, timestamp, market
        """
        await self._publish_execution_event(event)

    async def _publish_execution_event(self, event: dict[str, Any]) -> None:
        """
        체결 이벤트 Redis 발행

        Args:
            event: 체결 이벤트 데이터
        """
        try:
            await publish_execution_event(event)
        except Exception as e:
            logger.error(f"Failed to publish execution event: {e}", exc_info=True)

    async def start(self):
        """
        모니터링 시작

        WebSocket 연결, 메시지 수신 루프를 시작합니다.
        """
        logger.info("Starting KIS WebSocket monitor...")
        self.is_running = True

        await self._initialize_websocket()
        if self.websocket_client is None:
            raise RuntimeError("KIS WebSocket client initialization failed")

        await self.websocket_client.connect_and_subscribe()

        await self.websocket_client.listen()

    async def stop(self):
        """
        모니터링 정지

        WebSocket 연결, Redis 연결을 정리합니다.
        """
        logger.info("Stopping KIS WebSocket monitor...")
        self.is_running = False

        if self.websocket_client:
            await self.websocket_client.stop()

        await close_execution_redis()

        logger.info("KIS WebSocket monitor stopped")


async def main():
    """
    메인 엔트리 포인트

    로깅 설정 및 모니터 시작/종료를 담당합니다.
    """
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="auto-trader-kis-ws")

    monitor = KISWebSocketMonitor()

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        capture_exception(e, process="kis_websocket_monitor")
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await monitor.stop()
        logger.info("KIS WebSocket monitor exited gracefully")


if __name__ == "__main__":
    asyncio.run(main())
