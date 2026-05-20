"""Redis-backed model rate limiter for Gemini API (ROB-279).

Tracks per-model per-API-key rate-limit state in Redis so that 429 errors
from Gemini are recorded and consulted before each subsequent call.

Redis key structure:
  model_rate_limit:{model}:{masked_api_key}  — presence means limited
  model_retry_info:{model}:{masked_api_key}  — retry delay info (JSON)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

_DEFAULT_LIMIT_TTL_SECONDS = 60  # conservative: 1 minute per 429


class ModelRateLimiter:
    """Check and record Gemini model rate-limit state in Redis.

    Falls back gracefully when Redis is unavailable — never raises, just
    logs a warning so the LLM call path is not blocked by infra issues.
    """

    def __init__(
        self, redis_client=None, limit_ttl: int = _DEFAULT_LIMIT_TTL_SECONDS
    ) -> None:
        """
        Args:
            redis_client: An async Redis client (e.g. from ``aioredis``).
                          If None, a no-op mode is used (all checks return False).
            limit_ttl:    How many seconds a 429 lock is held.
        """
        self._redis = redis_client
        self._limit_ttl = limit_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_model_limited(self, model_name: str, api_key_hint: str) -> bool:
        """Return True if *model_name* for *api_key_hint* is currently rate-limited."""
        if self._redis is None:
            return False
        key = self._rate_limit_key(model_name, api_key_hint)
        try:
            return bool(await self._redis.exists(key))
        except Exception as exc:
            _logger.warning(
                "model_rate_limiter: Redis error checking limit (%s); allowing call",
                exc,
            )
            return False

    async def mark_limited(
        self,
        model_name: str,
        api_key_hint: str,
        retry_delay: int | None = None,
    ) -> None:
        """Record a 429 for *model_name* / *api_key_hint* in Redis."""
        if self._redis is None:
            return
        ttl = retry_delay if retry_delay and retry_delay > 0 else self._limit_ttl
        rate_key = self._rate_limit_key(model_name, api_key_hint)
        retry_key = self._retry_info_key(model_name, api_key_hint)
        try:
            await self._redis.setex(rate_key, ttl, "1")
            await self._redis.setex(
                retry_key,
                ttl,
                json.dumps({"retry_delay": ttl, "model": model_name}),
            )
            _logger.info(
                "model_rate_limiter: marked %s as rate-limited for %s (ttl=%ss)",
                model_name,
                api_key_hint,
                ttl,
            )
        except Exception as exc:
            _logger.warning("model_rate_limiter: Redis error recording limit (%s)", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rate_limit_key(model_name: str, api_key_hint: str) -> str:
        return f"model_rate_limit:{model_name}:{api_key_hint}"

    @staticmethod
    def _retry_info_key(model_name: str, api_key_hint: str) -> str:
        return f"model_retry_info:{model_name}:{api_key_hint}"
