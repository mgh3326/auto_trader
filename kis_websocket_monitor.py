#!/usr/bin/env python3
"""
KIS WebSocket Monitor - Main Entry Point

KIS 체결 WebSocket을 모니터링하여 체결 이벤트를 Redis pub/sub으로 발행하고
DCA 플랜 체결 단계를 자동 업데이트합니다.

사용법:
    python kis_websocket_monitor.py

종료: Ctrl+C (SIGINT) 또는 SIGTERM 신호
"""

import asyncio
import logging
import signal
import sys
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.services.dca_service import DcaService
from app.services.execution_event import (
    close_redis as close_execution_redis,
)
from app.services.execution_event import (
    publish_execution_event,
)
from app.services.kis_websocket import KISExecutionWebSocket
from app.services.openclaw_client import OpenClawClient

logger = logging.getLogger(__name__)


class KISWebSocketMonitor:
    """
    KIS WebSocket 모니터

    WebSocket 연결, 체결 처리, DCA 통합을 담당합니다.
    """

    def __init__(self):
        self.dca_service: DcaService | None = None
        self.websocket_client: KISExecutionWebSocket | None = None
        self.openclaw_client = OpenClawClient()
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

    async def _initialize_db(self):
        """
        데이터베이스 엔진 초기화

        Returns:
            AsyncSession: 비동기 DB 세션
        """
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        async_session_maker = sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        return async_session_maker()

    async def _initialize_dca_service(self, db_session: AsyncSession):
        """
        DCA 서비스 초기화

        Args:
            db_session: DB 세션
        """
        self.dca_service = DcaService(db_session)
        logger.info("DCA service initialized")

    async def _initialize_websocket(self):
        """
        KIS WebSocket 클라이언트 초기화

        체결 이벤트 콜백을 등록합니다.
        """
        self.websocket_client = KISExecutionWebSocket(
            on_execution=self._on_execution,
            mock_mode=settings.kis_ws_is_mock,
        )
        logger.info("KIS WebSocket client initialized")

    async def _on_execution(self, event: dict[str, Any]):
        """
        체결 이벤트 수신 처리

        DCA 체결 단계 업데이트 및 Redis 이벤트 발행을 수행합니다.
        order_id가 있는 경우에만 DCA 통합을 시도합니다.

        Args:
            event: 체결 이벤트 데이터
                symbol, side, order_id, filled_price, filled_qty, exec_time, timestamp, market
        """
        order_id = event.get("order_id")
        if not order_id:
            logger.debug("Execution event without order_id, skipping DCA update")
        else:
            try:
                await self._update_dca_step(order_id, event)
            except Exception as e:
                logger.warning(f"DCA step update failed: {e}, continuing...")

        await self._publish_execution_event(event)
        await self._notify_openclaw(event)

    async def _update_dca_step(self, order_id: str, event: dict[str, Any]) -> None:
        """
        DCA 체결 단계 업데이트

        order_id로 DCA step를 찾아 FILLED로 표시합니다.
        다음 pending step가 있으면 이벤트에 추가합니다.

        Args:
            order_id: 주문 ID
            event: 체결 이벤트 데이터
        """
        if not self.dca_service:
            return

        step = await self.dca_service.find_step_by_order_id(order_id)
        if not step:
            logger.debug(f"No DCA step found for order_id={order_id}")
            return

        await self.dca_service.mark_step_filled(
            step_id=step.id,
            filled_price=Decimal(str(event.get("filled_price", 0))),
            filled_qty=Decimal(str(event.get("filled_qty", 0))),
        )

        logger.info(
            f"DCA step marked as filled: step_id={step.id}, "
            f"order_id={order_id}, plan_id={step.plan_id}"
        )

        next_step = await self.dca_service.get_next_pending_step(step.plan_id)
        if next_step:
            event["dca_next_step"] = {
                "plan_id": next_step.plan_id,
                "step_number": next_step.step_number,
                "target_price": float(next_step.target_price),
                "target_quantity": str(next_step.target_quantity),
            }
            logger.info(
                f"Next pending step added to event: plan_id={next_step.plan_id}, "
                f"step_number={next_step.step_number}"
            )

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

    async def _notify_openclaw(self, event: dict[str, Any]) -> None:
        """OpenClaw execution 알림 전송 (실패해도 체결 파이프라인은 유지)."""
        try:
            sent = await self.openclaw_client.send_execution_notification(event)
            if not sent:
                logger.debug(
                    "OpenClaw execution notification skipped/failed: symbol=%s",
                    event.get("symbol"),
                )
        except Exception as e:
            logger.warning(f"OpenClaw notification failed (non-fatal): {e}")

    async def start(self):
        """
        모니터링 시작

        WebSocket 연결, 메시지 수신 루프를 시작합니다.
        """
        logger.info("Starting KIS WebSocket monitor...")
        self.is_running = True

        async_session_maker = await self._initialize_db()

        async with async_session_maker() as db_session:
            await self._initialize_dca_service(db_session)
            await self._initialize_websocket()

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

    monitor = KISWebSocketMonitor()

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await monitor.stop()
        logger.info("KIS WebSocket monitor exited gracefully")


if __name__ == "__main__":
    asyncio.run(main())
