import logging
import httpx
import redis.asyncio as redis
from app.core.config import settings
from .constants import (
    APPROVAL_KEY_CACHE_KEY,
    APPROVAL_KEY_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# 전역 Redis 클라이언트 (지연 초기화)
_redis_client: redis.Redis | None = None


async def _get_redis_client() -> redis.Redis:
    """
    Redis 클라이언트 가져오기 (지연 초기화)

    Returns:
        redis.Redis: Redis 클라이언트

    Raises:
        redis.RedisError: Redis 연결 실패 시
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


async def close_approval_key_redis() -> None:
    """
    Approval Key 캐시용 Redis 클라이언트 정리

    모듈 전역 Redis 클라이언트를 안전하게 종료합니다.
    idempotent하며 여러 번 호출해도 안전합니다.
    """
    global _redis_client

    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None


def _is_valid_approval_key(key: str | None) -> bool:
    """
    Approval Key 유효성 검사

    None, 빈 문자열, 공백만 있는 문자열을 무효로 처리합니다.

    Args:
        key: 검사할 Approval Key

    Returns:
        bool: 유효한 키면 True, 아니면 False
    """
    return key is not None and bool(key.strip())


async def get_approval_key() -> str:
    """
    KIS WebSocket Approval Key 발급

    Approval Key는 24시간 유효하며, 23시간 캐시하여 재발급을 줄입니다.

    Returns:
        str: Approval Key

    Raises:
        Exception: Approval Key 발급 실패 시
    """
    approval_key = await _get_cached_approval_key()

    if not _is_valid_approval_key(approval_key):
        approval_key = await _issue_approval_key()
        await _cache_approval_key(approval_key)

    assert approval_key is not None  # _issue_approval_key() guarantees str return
    return approval_key


async def _get_cached_approval_key() -> str | None:
    """
    캐시된 Approval Key 조회 (만료 체크 포함)

    Returns:
        str | None: 캐시된 Approval Key (없거나 만료된 경우 None)

    Raises:
        redis.RedisError: Redis 접근 실패 시 (엄격 실패 정책)
    """
    redis_client = await _get_redis_client()
    cached_key = await redis_client.get(APPROVAL_KEY_CACHE_KEY)
    return cached_key


async def _cache_approval_key(approval_key: str) -> None:
    """
    Approval Key 캐싱 (23시간 TTL)

    Args:
        approval_key: 캐싱할 Approval Key

    Raises:
        redis.RedisError: Redis 접근 실패 시 (엄격 실패 정책)
    """
    redis_client = await _get_redis_client()
    await redis_client.set(
        APPROVAL_KEY_CACHE_KEY, approval_key, ex=APPROVAL_KEY_TTL_SECONDS
    )
    logger.info("KIS Approval Key cached in Redis (TTL: 23h)")


async def _issue_approval_key() -> str:
    """
    KIS Approval Key 발급 API 호출

    Returns:
        str: 발급된 Approval Key

    Raises:
        Exception: HTTP 요청 실패 또는 응답에 approval_key 없음
    """
    base_url = "https://openapi.koreainvestment.com:9443"
    path = "/oauth2/Approval"
    url = f"{base_url}{path}"

    headers = {
        "Content-Type": "application/json",
    }

    request_body = {
        "grant_type": "client_credentials",
        "appkey": settings.kis_app_key,
        "secretkey": settings.kis_app_secret,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, headers=headers, json=request_body, timeout=10
        )
        response.raise_for_status()
        data = response.json()

    issued_key = data.get("approval_key")
    if not issued_key:
        raise Exception("Approval Key not found in response")

    logger.info("KIS Approval Key issued successfully")
    return issued_key
