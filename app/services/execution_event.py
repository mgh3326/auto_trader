"""
Execution Event Redis Publisher

체결 이벤트를 Redis pub/sub으로 발행합니다.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)


_redis_client: redis.Redis | None = None


async def _get_redis_client() -> redis.Redis:
    """
    Redis 클라이언트 가져오기 (지연 초기화)

    Returns:
        redis.Redis: Redis 클라이언트
    """
    global _redis_client

    if _redis_client is None:
        redis_url = settings.get_redis_url()
        _redis_client = redis.from_url(
            redis_url,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            decode_responses=True,
        )

    return _redis_client


def _serialize_for_redis(value: Any) -> Any:
    """
    Redis JSON 직렬화를 위한 값 변환

    Args:
        value: 직렬화할 값

    Returns:
        Any: 직렬화 가능한 형태로 변환된 값
    """
    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (list, dict)):
        if isinstance(value, dict):
            return {k: _serialize_for_redis(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_serialize_for_redis(item) for item in value]

    return value


async def publish_execution_event(event: dict[str, Any]) -> None:
    """
    체결 이벤트 발행

    market 값에 따라 채널을 라우팅하여 발행합니다.

    Args:
        event: 체결 이벤트 데이터
            필수 필드: type, market, symbol, side, order_id, filled_price, filled_qty, exec_time, timestamp
            옵션 필드: dca_next_step (plan_id, step_number, target_price, target_quantity)

    Raises:
        Exception: Redis 발행 실패 시
    """
    market = event.get("market", "unknown")
    channel = f"execution:{market}"

    serialized_event = _serialize_for_redis(event)
    message = json.dumps(serialized_event, ensure_ascii=False)

    redis_client = await _get_redis_client()

    try:
        await redis_client.publish(channel, message)
        logger.info(
            f"Published execution event to channel {channel}: {event.get('order_id')}"
        )
    except Exception as e:
        logger.error(f"Failed to publish execution event: {e}", exc_info=True)
        raise


async def close_redis() -> None:
    """
    Redis 연결 종료

    Graceful shutdown 시 호출하여 연결을 명시적으로 정리합니다.
    """
    global _redis_client

    if _redis_client:
        try:
            await _redis_client.close()
            _redis_client = None
            logger.info("Redis connection closed")
        except Exception as e:
            logger.error(f"Error closing Redis connection: {e}", exc_info=True)
