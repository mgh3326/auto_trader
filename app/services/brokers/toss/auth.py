from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

import redis.asyncio as redis
from pydantic import SecretStr

from app.core.config import settings, validate_toss_api_config
from app.services.brokers.toss.errors import (
    TossApiDisabled,
    TossMissingCredentials,
    TossTokenIssuanceUnavailable,
)
from app.services.brokers.toss.rate_limiter import TossApiGroup, TossRateLimiter
from app.services.brokers.toss.transport import DEFAULT_TOSS_BASE_URL, build_toss_client

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_BUFFER_SECONDS: Final[int] = 120
TOKEN_LOCK_TTL_SECONDS: Final[int] = 30
TOKEN_WAIT_TIMEOUT_SECONDS: float = 5.0
TOKEN_WAIT_POLL_SECONDS: float = 0.05

_redis_client: redis.Redis | None = None


@dataclass(frozen=True)
class TossToken:
    access_token: str
    expires_in: int


async def _get_redis_client() -> redis.Redis:
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


async def close_toss_token_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None


def _client_fingerprint(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


class TossOAuthTokenManager:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        base_url: str = DEFAULT_TOSS_BASE_URL,
        rate_limiter: TossRateLimiter | None = None,
    ) -> None:
        if not client_id.strip():
            raise TossMissingCredentials("TOSS_API_CLIENT_ID is empty")
        secret_value = client_secret.get_secret_value()
        if not secret_value.strip():
            raise TossMissingCredentials("TOSS_API_CLIENT_SECRET is empty")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url
        self._rate_limiter = rate_limiter or TossRateLimiter()
        self._namespace = f"toss:oauth:{_client_fingerprint(client_id)}"
        self.token_key = f"{self._namespace}:access_token"
        self.lock_key = f"{self._namespace}:lock"

    def __repr__(self) -> str:
        return (
            f"<TossOAuthTokenManager base_url={self._base_url!r} "
            f"client_id_fp={_client_fingerprint(self._client_id)!r}>"
        )

    @classmethod
    def from_settings(cls, settings_obj: Any = settings) -> TossOAuthTokenManager:
        if not bool(getattr(settings_obj, "toss_api_enabled", False)):
            raise TossApiDisabled(
                "Toss API is disabled: TOSS_API_ENABLED is not truthy"
            )
        missing = validate_toss_api_config(settings_obj)
        if missing:
            raise TossMissingCredentials(
                "Toss API is disabled or missing required configuration: "
                + ", ".join(missing)
            )
        secret = settings_obj.toss_api_client_secret
        if not isinstance(secret, SecretStr):
            secret = SecretStr(str(secret))
        base_url = (
            getattr(settings_obj, "toss_api_base_url", None) or DEFAULT_TOSS_BASE_URL
        )
        return cls(
            client_id=str(settings_obj.toss_api_client_id),
            client_secret=secret,
            base_url=str(base_url),
        )

    async def get_access_token(self, *, force_reissue: bool = False) -> str:
        if not force_reissue:
            cached = await self._get_cached_token()
            if cached is not None:
                return cached
        return await self._issue_single_flight(force_reissue=force_reissue)

    async def _get_cached_token(self) -> str | None:
        redis_client = await _get_redis_client()
        raw = await redis_client.get(self.token_key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            access_token = data["access_token"]
            expires_at = float(data["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if time.time() >= expires_at - TOKEN_EXPIRY_BUFFER_SECONDS:
            return None
        return str(access_token)

    async def _cache_token(self, token: TossToken) -> None:
        now = time.time()
        expires_at = now + int(token.expires_in)
        payload = {"access_token": token.access_token, "expires_at": expires_at}
        redis_client = await _get_redis_client()
        ttl = max(int(token.expires_in), 1)
        await redis_client.set(self.token_key, json.dumps(payload), ex=ttl)

    async def _issue_single_flight(self, *, force_reissue: bool = False) -> str:
        redis_client = await _get_redis_client()
        lock_token = str(uuid.uuid4())
        acquired = await redis_client.set(
            self.lock_key,
            lock_token,
            nx=True,
            ex=TOKEN_LOCK_TTL_SECONDS,
        )
        if acquired:
            try:
                if force_reissue:
                    await redis_client.delete(self.token_key)
                else:
                    cached = await self._get_cached_token()
                    if cached is not None:
                        return cached
                issued = await self._issue_token()
                await self._cache_token(issued)
                logger.info("Toss OAuth token issued and cached")
                return issued.access_token
            finally:
                await self._release_lock(redis_client, lock_token)
        waited = await self._wait_for_cached_token()
        if waited is not None:
            return waited
        raise TossTokenIssuanceUnavailable(
            "Toss OAuth token issuance contended; no cached token after bounded wait"
        )

    async def _wait_for_cached_token(self) -> str | None:
        deadline = time.monotonic() + TOKEN_WAIT_TIMEOUT_SECONDS
        while True:
            cached = await self._get_cached_token()
            if cached is not None:
                return cached
            if time.monotonic() >= deadline:
                return None
            poll = max(float(TOKEN_WAIT_POLL_SECONDS), 0.0)
            await asyncio.sleep(poll + random.uniform(0.0, poll))

    async def _release_lock(self, redis_client: redis.Redis, lock_token: str) -> None:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            await redis_client.eval(script, 1, self.lock_key, lock_token)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Toss OAuth lock release best-effort failure: %s", exc)

    async def _issue_token(self) -> TossToken:
        await self._rate_limiter.acquire(TossApiGroup.AUTH)
        async with build_toss_client(base_url=self._base_url) as client:
            response = await client.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret.get_secret_value(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        response.raise_for_status()
        payload = response.json()
        return TossToken(
            access_token=str(payload["access_token"]),
            expires_in=int(payload["expires_in"]),
        )
