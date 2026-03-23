"""
Crypto Trade Cooldown Service

Manages stop-loss cooldown state for crypto symbols using Redis.
8-day cooldown period to prevent immediate re-entry after stop-loss sells.
"""

import logging
from collections.abc import Iterable

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# Constants for Phase 2 strategy
STOP_LOSS_COOLDOWN_DAYS = 8
STOP_LOSS_COOLDOWN_TTL_SECONDS = STOP_LOSS_COOLDOWN_DAYS * 24 * 60 * 60


def _key(symbol: str) -> str:
    """Generate Redis key for stop-loss cooldown."""
    normalized = symbol.upper().strip()
    return f"crypto:stop_loss_cooldown:{normalized}"


class CryptoTradeCooldownService:
    """Service for managing crypto stop-loss cooldown state."""

    def __init__(self) -> None:
        self._redis_client: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis_client is None:
            self._redis_client = await redis.from_url(
                settings.get_redis_url(),
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        return self._redis_client

    async def is_in_cooldown(self, symbol: str) -> bool:
        """
        Check if a symbol is currently in stop-loss cooldown.

        Args:
            symbol: The crypto symbol (e.g., "KRW-BTC")

        Returns:
            True if in cooldown, False otherwise.
            Returns False on Redis errors (degrades safely).
        """
        try:
            redis_client = await self._get_redis()
            result = await redis_client.get(_key(symbol))
            return bool(result)
        except Exception:
            logger.warning("crypto stop-loss cooldown read failed", exc_info=True)
            return False

    async def record_stop_loss(self, symbol: str) -> None:
        """
        Record a stop-loss event for a symbol, starting the cooldown period.

        Args:
            symbol: The crypto symbol (e.g., "KRW-BTC")

        Note:
            Does not raise on Redis errors (degrades safely).
        """
        try:
            redis_client = await self._get_redis()
            await redis_client.set(
                _key(symbol),
                "1",
                ex=STOP_LOSS_COOLDOWN_TTL_SECONDS,
            )
            logger.info(f"Recorded stop-loss cooldown for {symbol}")
        except Exception:
            logger.warning("crypto stop-loss cooldown write failed", exc_info=True)

    async def get_remaining_ttl_seconds(self, symbol: str) -> int | None:
        """
        Get the remaining cooldown TTL in seconds.

        Args:
            symbol: The crypto symbol (e.g., "KRW-BTC")

        Returns:
            Remaining seconds if in cooldown, None if not in cooldown.
            Returns None on Redis errors.
        """
        try:
            redis_client = await self._get_redis()
            ttl = await redis_client.ttl(_key(symbol))
            if ttl > 0:
                return ttl
            return None
        except Exception:
            logger.warning("crypto stop-loss cooldown TTL read failed", exc_info=True)
            return None

    async def filter_symbols_in_cooldown(self, symbols: Iterable[str]) -> set[str]:
        """Return the subset of symbols that are currently in cooldown."""
        normalized = [symbol.upper().strip() for symbol in symbols if symbol]
        if not normalized:
            return set()

        try:
            redis_client = await self._get_redis()
            values = await redis_client.mget([_key(symbol) for symbol in normalized])
            return {
                symbol
                for symbol, value in zip(normalized, values, strict=False)
                if value
            }
        except Exception:
            logger.warning("crypto stop-loss cooldown batch read failed", exc_info=True)
            return set()
