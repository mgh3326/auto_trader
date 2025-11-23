"""Redis-based session blacklist for user deactivation."""
from typing import Optional

import redis.asyncio as redis

from app.core.config import settings


class SessionBlacklist:
    """Redis를 활용한 세션 블랙리스트 관리자."""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or settings.get_redis_url()
        self.redis_client: Optional[redis.Redis] = None
        self._blacklist_key_prefix = "session_blacklist:user:"

    async def _get_redis_client(self) -> redis.Redis:
        """Redis 클라이언트를 가져오거나 생성."""
        if self.redis_client is None:
            self.redis_client = redis.from_url(
                self.redis_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        return self.redis_client

    async def close(self):
        """Redis 연결 종료."""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None

    async def blacklist_user(self, user_id: int, ttl: int = 86400 * 7) -> bool:
        """
        사용자를 블랙리스트에 추가 (세션 무효화).

        Args:
            user_id: 사용자 ID
            ttl: TTL (초, 기본 7일)

        Returns:
            성공 여부
        """
        try:
            client = await self._get_redis_client()
            key = f"{self._blacklist_key_prefix}{user_id}"
            await client.set(key, "1", ex=ttl)
            return True
        except Exception:
            return False

    async def is_blacklisted(self, user_id: int) -> bool:
        """
        사용자가 블랙리스트에 있는지 확인.

        Args:
            user_id: 사용자 ID

        Returns:
            블랙리스트 여부
        """
        try:
            client = await self._get_redis_client()
            key = f"{self._blacklist_key_prefix}{user_id}"
            result = await client.get(key)
            return result is not None
        except Exception:
            # Redis 장애 시 보수적으로 False 반환 (차단하지 않음)
            return False

    async def remove_from_blacklist(self, user_id: int) -> bool:
        """
        사용자를 블랙리스트에서 제거.

        Args:
            user_id: 사용자 ID

        Returns:
            성공 여부
        """
        try:
            client = await self._get_redis_client()
            key = f"{self._blacklist_key_prefix}{user_id}"
            await client.delete(key)
            return True
        except Exception:
            return False


# 싱글톤 인스턴스
_session_blacklist: Optional[SessionBlacklist] = None


def get_session_blacklist() -> SessionBlacklist:
    """세션 블랙리스트 싱글톤 인스턴스 반환."""
    global _session_blacklist
    if _session_blacklist is None:
        _session_blacklist = SessionBlacklist()
    return _session_blacklist
