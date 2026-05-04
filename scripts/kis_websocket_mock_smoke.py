#!/usr/bin/env python3
"""KIS WebSocket Mock Smoke

ROB-104: 운영 서버에서 mock KIS WebSocket 의 approval-key 발급, 연결,
TR 구독 핸드셰이크가 정상 동작하는지 빠르게 검증한다. 체결 콜백, 주문, Redis
publish 는 수행하지 않는다.

Exit codes:
    0  - smoke 성공
    1  - 예기치 못한 예외
    2  - subscription ACK 실패 (KISSubscriptionAckError)
    3  - 연결 실패 (RuntimeError "WebSocket connection not established" 등)
    4  - 설정 누락 (KIS_WS_HTS_ID 미설정)

사용법:
    uv run python -m scripts.kis_websocket_mock_smoke
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from app.core.config import settings
from app.services.kis_websocket import (
    KISExecutionWebSocket,
    KISSubscriptionAckError,
)

logger = logging.getLogger(__name__)


def _noop_on_execution(_event: dict[str, Any]) -> None:
    """Smoke callback — drop everything on the floor.

    Smoke must never publish, mutate, or place orders. We register a callback
    only because the client requires one; it is intentionally a no-op.
    """
    return None


async def run_smoke() -> int:
    """Run the bounded mock smoke handshake.

    Returns exit code (see module docstring).
    """
    if not str(settings.kis_ws_hts_id or "").strip():
        logger.error(
            "KIS_WS_HTS_ID is not configured; cannot run mock smoke handshake"
        )
        return 4

    client = KISExecutionWebSocket(
        on_execution=_noop_on_execution,
        mock_mode=True,
        account_mode="kis_mock",
    )
    # Smoke must terminate after a single connect; bound the reconnect loop.
    client.is_running = True
    client.max_reconnect_attempts = 1

    try:
        logger.info(
            "KIS mock smoke starting: account_mode=%s mock_mode=%s",
            client.account_mode,
            client.mock_mode,
        )
        await client.connect_and_subscribe()
    except KISSubscriptionAckError as e:
        logger.error(
            "KIS mock smoke FAILED: subscription ACK error tr_id=%s msg_cd=%s msg1=%s",
            e.tr_id,
            e.msg_cd,
            e.msg1,
        )
        return 2
    except RuntimeError as e:
        logger.error("KIS mock smoke FAILED: connection error: %s", e)
        return 3
    except Exception:
        logger.exception("KIS mock smoke FAILED: unexpected error")
        return 1
    finally:
        try:
            await client.stop()
        except Exception:
            logger.exception("Failed to stop KIS mock smoke client cleanly")

    logger.info(
        "KIS mock smoke OK: handshake complete (no orders, no callback wiring)"
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return asyncio.run(run_smoke())


if __name__ == "__main__":
    sys.exit(main())
