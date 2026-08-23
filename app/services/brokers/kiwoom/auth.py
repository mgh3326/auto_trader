# app/services/brokers/kiwoom/auth.py
"""Kiwoom OAuth token issuance and Redis-backed single-flight cache.

Uses ``expires_dt`` returned by Kiwoom (``YYYYMMDDHHMMSS``) to schedule
refreshes ``TOKEN_REFRESH_LEEWAY_SECONDS`` before expiry. Token issuance is
serialized across processes with the same Redis lock / failed-token
double-check pattern used by Toss OAuth. Logs intentionally omit the token,
app secret and full response body.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
import redis.asyncio as redis

from app.services.brokers.kiwoom import constants

_log = logging.getLogger(__name__)

TOKEN_LOCK_TTL_SECONDS: Final[int] = 30
TOKEN_WAIT_TIMEOUT_SECONDS: float = 5.0
TOKEN_WAIT_POLL_SECONDS: float = 0.05

_REDIS_MAX_CONNECTIONS_DEFAULT: Final[int] = 20
_REDIS_SOCKET_TIMEOUT_DEFAULT: Final[float] = 5.0
_REDIS_SOCKET_CONNECT_TIMEOUT_DEFAULT: Final[float] = 5.0
_settings_redis_clients: dict[tuple[str, int, float, float], redis.Redis] = {}


@dataclass(frozen=True)
class KiwoomToken:
    access_token: str
    expires_at: dt.datetime


class KiwoomTokenIssuanceUnavailable(RuntimeError):
    """Raised when another issuer never publishes a usable token."""


class KiwoomRedisConfigurationError(RuntimeError):
    """Raised with setting names only when scoped Redis configuration is invalid."""


class _RedisSettings(Protocol):
    redis_max_connections: int
    redis_socket_timeout: float
    redis_socket_connect_timeout: float

    def get_redis_url(self) -> str: ...


def _positive_number_setting(name: str, value: object, default: float) -> float:
    if value is None:
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise KiwoomRedisConfigurationError(f"invalid numeric setting: {name}") from exc
    if numeric <= 0:
        raise KiwoomRedisConfigurationError(f"non-positive numeric setting: {name}")
    return numeric


def redis_client_from_settings(settings_obj: _RedisSettings) -> redis.Redis:
    """Build Kiwoom's mandatory Redis client from the canonical Settings path.

    The OAuth cache is deliberately not optional: a missing or unreachable Redis
    still prevents token resolution before a broker request is dispatched.
    """

    redis_url = str(settings_obj.get_redis_url() or "").strip()
    if not redis_url:
        raise KiwoomRedisConfigurationError("missing required setting: REDIS_URL")
    max_connections = _positive_number_setting(
        "REDIS_MAX_CONNECTIONS",
        getattr(settings_obj, "redis_max_connections", None),
        float(_REDIS_MAX_CONNECTIONS_DEFAULT),
    )
    socket_timeout = _positive_number_setting(
        "REDIS_SOCKET_TIMEOUT",
        getattr(settings_obj, "redis_socket_timeout", None),
        _REDIS_SOCKET_TIMEOUT_DEFAULT,
    )
    socket_connect_timeout = _positive_number_setting(
        "REDIS_SOCKET_CONNECT_TIMEOUT",
        getattr(settings_obj, "redis_socket_connect_timeout", None),
        _REDIS_SOCKET_CONNECT_TIMEOUT_DEFAULT,
    )
    key = (redis_url, int(max_connections), socket_timeout, socket_connect_timeout)
    if key not in _settings_redis_clients:
        _settings_redis_clients[key] = redis.from_url(
            redis_url,
            max_connections=int(max_connections),
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            decode_responses=True,
        )
    return _settings_redis_clients[key]


def _client_fingerprint(app_key: str) -> str:
    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()[:16]


def _parse_expires_dt(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC)


class KiwoomAuthClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = constants.DEFAULT_TIMEOUT,
        redis_client: redis.Redis | None = None,
        redis_settings: _RedisSettings | None = None,
    ) -> None:
        if str(base_url).rstrip("/") != constants.MOCK_BASE_URL:
            raise ValueError(
                f"KiwoomAuthClient is mock-only; got base_url={base_url!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._app_key = app_key
        self._app_secret = app_secret
        self._transport = transport
        self._timeout = timeout
        self._redis_client = redis_client
        self._redis_settings = redis_settings
        namespace = f"kiwoom:oauth:{_client_fingerprint(app_key)}"
        self.token_key = f"{namespace}:access_token"
        self.lock_key = f"{namespace}:lock"

    async def get_token(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        if not force_reissue:
            cached = await self._get_cached_token()
            if cached is not None:
                return cached
        elif failed_token is not None:
            cached = await self._get_cached_token()
            if cached is not None and cached != failed_token:
                return cached
        return await self._issue_single_flight(
            force_reissue=force_reissue,
            failed_token=failed_token,
        )

    async def _get_redis(self) -> redis.Redis:
        if self._redis_client is not None:
            return self._redis_client
        if self._redis_settings is None:
            raise KiwoomRedisConfigurationError(
                "missing required Settings-backed Redis configuration"
            )
        self._redis_client = redis_client_from_settings(self._redis_settings)
        return self._redis_client

    async def _get_cached_token(self) -> str | None:
        redis_client = await self._get_redis()
        raw = await redis_client.get(self.token_key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            access_token = data["access_token"]
            expires_at = float(data["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if time.time() >= expires_at - constants.TOKEN_REFRESH_LEEWAY_SECONDS:
            return None
        return str(access_token)

    async def _cache_token(self, token: KiwoomToken) -> None:
        redis_client = await self._get_redis()
        expires_at = token.expires_at.timestamp()
        ttl = max(int(expires_at - time.time()), 1)
        payload = {
            "access_token": token.access_token,
            "expires_at": expires_at,
        }
        await redis_client.set(self.token_key, json.dumps(payload), ex=ttl)

    async def _issue_single_flight(
        self, *, force_reissue: bool = False, failed_token: str | None = None
    ) -> str:
        redis_client = await self._get_redis()
        lock_token = str(uuid.uuid4())
        acquired = await redis_client.set(
            self.lock_key,
            lock_token,
            nx=True,
            ex=TOKEN_LOCK_TTL_SECONDS,
        )
        if acquired:
            try:
                cached = await self._get_cached_token()
                if cached is not None:
                    if not force_reissue:
                        return cached
                    if failed_token is not None and cached != failed_token:
                        return cached
                if force_reissue:
                    await redis_client.delete(self.token_key)
                issued = await self._issue_token()
                await self._cache_token(issued)
                _log.info("Kiwoom OAuth token issued and cached")
                return issued.access_token
            finally:
                await self._release_lock(redis_client, lock_token)

        waited = await self._wait_for_cached_token(failed_token=failed_token)
        if waited is not None:
            return waited
        raise KiwoomTokenIssuanceUnavailable(
            "Kiwoom OAuth token issuance contended; no cached token after bounded wait"
        )

    async def _wait_for_cached_token(
        self, *, failed_token: str | None = None
    ) -> str | None:
        deadline = time.monotonic() + TOKEN_WAIT_TIMEOUT_SECONDS
        while True:
            cached = await self._get_cached_token()
            if cached is not None and cached != failed_token:
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
            _log.debug(
                "Kiwoom OAuth lock release best-effort failure type=%s",
                type(exc).__name__,
            )

    async def _issue_token(self) -> KiwoomToken:
        body = {
            "grant_type": constants.OAUTH_GRANT_TYPE,
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            response = await client.post(
                constants.OAUTH_PATH,
                json=body,
                headers={"Content-Type": constants.OAUTH_CONTENT_TYPE},
            )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if int(payload.get("return_code", -1)) != constants.SUCCESS_RETURN_CODE:
            _log.warning(
                "Kiwoom OAuth refresh non-zero return_code=%s",
                payload.get("return_code"),
            )
        token = str(payload.get("token") or "").strip()
        expires_raw = str(payload.get("expires_dt") or "").strip()
        if not token or not expires_raw:
            raise RuntimeError("Kiwoom OAuth response missing token/expires_dt")
        _log.debug("Kiwoom OAuth token refreshed (expires_at hidden)")
        return KiwoomToken(
            access_token=token,
            expires_at=_parse_expires_dt(expires_raw),
        )
