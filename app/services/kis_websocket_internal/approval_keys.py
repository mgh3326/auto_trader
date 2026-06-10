import asyncio
import logging
import random
import time
import uuid
from urllib.parse import urlparse

import httpx
import redis.asyncio as redis

from app.core.config import settings, validate_kis_mock_config

from .constants import (
    APPROVAL_ENDPOINT_HOSTS,
    APPROVAL_KEY_CACHE_KEYS,
    APPROVAL_KEY_LOCK_CACHE_KEYS,
    APPROVAL_KEY_LOCK_TTL_SECONDS,
    APPROVAL_KEY_TTL_SECONDS,
    APPROVAL_KEY_WAIT_POLL_SECONDS,
    APPROVAL_KEY_WAIT_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# WebSocket approval keys are only issued for KIS environments.
_SUPPORTED_ACCOUNT_MODES = ("kis_live", "kis_mock")


class ApprovalKeyIssuanceUnavailable(RuntimeError):
    """Raised when a cold-start contender cannot obtain an approval key.

    Another process holds the single-flight issuance lock but did not publish a
    cached key within the bounded wait. The contender deliberately fails/backs
    off here rather than independently issuing a second approval key (which is
    exactly the cold-start churn ROB-262 prevents).
    """


def _resolve_ws_account_mode(account_mode: str) -> str:
    """Validate the account mode for WebSocket approval-key issuance.

    Only ``kis_live`` / ``kis_mock`` are supported — anything else fails closed.
    """
    if account_mode not in _SUPPORTED_ACCOUNT_MODES:
        raise ValueError(
            f"account_mode must be one of {_SUPPORTED_ACCOUNT_MODES}, got {account_mode!r}"
        )
    return account_mode


def _resolve_approval_credentials(account_mode: str) -> tuple[str, str, str]:
    """Return ``(base_url, appkey, secret)`` for the mode.

    Mock fails closed via ``validate_kis_mock_config`` and surfaces only the
    missing env var *names* — never the configured secret values.
    """
    if account_mode == "kis_mock":
        missing = validate_kis_mock_config()
        if missing:
            raise ValueError(
                "KIS mock WebSocket approval key requires configuration: "
                + ", ".join(missing)
            )
        return (
            settings.kis_mock_base_url,
            settings.kis_mock_app_key,
            settings.kis_mock_app_secret,
        )
    return settings.kis_base_url, settings.kis_app_key, settings.kis_app_secret


def _assert_approval_endpoint_host(account_mode: str, base_url: str) -> None:
    """Fail-closed: the approval endpoint host:port must match the mode's allowlist."""
    parsed = urlparse(base_url)
    host_port = f"{parsed.hostname}:{parsed.port}"
    allowed = APPROVAL_ENDPOINT_HOSTS[account_mode]
    if host_port != allowed:
        raise ValueError(
            f"approval endpoint {host_port!r} not allowed for {account_mode} "
            f"(expected {allowed!r})"
        )


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


async def get_approval_key(account_mode: str = "kis_live") -> str:
    """
    KIS WebSocket Approval Key 발급 (account-mode aware)

    Approval Key는 24시간 유효하며, 23시간 캐시하여 재발급을 줄입니다.
    live / mock 은 별도 Redis 네임스페이스 + 별도 endpoint/credential 을 사용합니다.

    Args:
        account_mode: "kis_live" (default) 또는 "kis_mock".

    Returns:
        str: Approval Key

    Raises:
        ValueError: account_mode 미지원 또는 mock 설정 누락 시 (fail-closed)
        Exception: Approval Key 발급 실패 시
    """
    account_mode = _resolve_ws_account_mode(account_mode)
    approval_key = await _get_cached_approval_key(account_mode)

    if _is_valid_approval_key(approval_key):
        logger.info("KIS approval key cache hit: account_mode=%s", account_mode)
        assert approval_key is not None  # narrowed by _is_valid_approval_key
        return approval_key

    logger.info("KIS approval key cache miss: account_mode=%s", account_mode)
    return await _issue_approval_key_single_flight(account_mode)


async def invalidate_and_reissue_approval_key(account_mode: str = "kis_live") -> str:
    """Controlled, single-flight reissue of the approval key (OPSP0011 path).

    This is the ONLY sanctioned path that discards a cached approval key. It runs
    under the same single-flight lock as cold issuance: the lock holder clears the
    known-bad key, issues exactly one fresh key, and re-caches it; concurrent
    owners wait and reuse the holder's fresh key instead of each hammering the
    approval endpoint. If the holder's reissue fails, the bad key is left cleared
    so contenders fail/back off rather than reusing a key KIS already rejected.

    Returns:
        str: the freshly issued (or holder-published) approval key.

    Raises:
        ApprovalKeyIssuanceUnavailable: contender timed out waiting for the key.
        Exception: the approval endpoint call failed (propagated to the caller).
    """
    account_mode = _resolve_ws_account_mode(account_mode)
    logger.info(
        "KIS approval key controlled reissue requested: account_mode=%s", account_mode
    )
    return await _issue_approval_key_single_flight(account_mode, force_reissue=True)


async def _issue_approval_key_single_flight(
    account_mode: str, *, force_reissue: bool = False
) -> str:
    """Issue the approval key under a Redis single-flight lock.

    Lock holder: (optionally) invalidates the cached key, calls the approval
    endpoint exactly once, caches the result, and always releases the lock.
    Contender (lock held elsewhere): waits with bounded, jittered retries and
    reuses the holder's cached key — never issues independently.
    """
    account_mode = _resolve_ws_account_mode(account_mode)
    redis_client = await _get_redis_client()
    lock_token = await _acquire_issuance_lock(redis_client, account_mode)

    if lock_token is not None:
        lock_key = APPROVAL_KEY_LOCK_CACHE_KEYS[account_mode]
        try:
            if force_reissue:
                # Drop the KIS-rejected key first so a failed reissue cannot leave
                # contenders reusing a bad key.
                await _invalidate_cached_approval_key(account_mode)
            else:
                # Double-checked locking: another holder may have published a key
                # between our cache miss and acquiring the lock.
                cached = await _get_cached_approval_key(account_mode)
                if _is_valid_approval_key(cached):
                    logger.info(
                        "KIS approval key reused after acquiring lock "
                        "(published by prior holder): account_mode=%s",
                        account_mode,
                    )
                    assert cached is not None
                    return cached

            logger.info(
                "KIS approval key issuance lock acquired; calling approval "
                "endpoint once: account_mode=%s force_reissue=%s",
                account_mode,
                force_reissue,
            )
            issued = await _issue_approval_key(account_mode)
            await _cache_approval_key(issued, account_mode)
            return issued
        finally:
            await _release_issuance_lock(redis_client, lock_key, lock_token)

    logger.info(
        "KIS approval key issuance lock held by another owner; waiting to reuse "
        "cached key: account_mode=%s",
        account_mode,
    )
    waited_key = await _wait_for_cached_approval_key(account_mode)
    if _is_valid_approval_key(waited_key):
        logger.info(
            "KIS approval key reused after waiting on issuance lock: account_mode=%s",
            account_mode,
        )
        assert waited_key is not None
        return waited_key

    logger.warning(
        "KIS approval key issuance contended and no cached key appeared within "
        "%.1fs; backing off without independent issuance: account_mode=%s",
        APPROVAL_KEY_WAIT_TIMEOUT_SECONDS,
        account_mode,
    )
    raise ApprovalKeyIssuanceUnavailable(
        f"approval key issuance contended for {account_mode}; "
        "no cached key after bounded wait"
    )


async def _acquire_issuance_lock(
    redis_client: redis.Redis, account_mode: str
) -> str | None:
    """Acquire the single-flight issuance lock via ``SET NX EX``.

    Returns a unique token on success (caller must release with that token), or
    None if another owner holds the lock. Redis errors propagate (fail-closed),
    matching the cache-read policy of this module.
    """
    lock_key = APPROVAL_KEY_LOCK_CACHE_KEYS[account_mode]
    lock_token = f"{uuid.uuid4()}"
    acquired = await redis_client.set(
        lock_key,
        lock_token,
        nx=True,
        ex=max(int(APPROVAL_KEY_LOCK_TTL_SECONDS), 1),
    )
    if acquired:
        logger.info(
            "KIS approval key issuance lock acquired: account_mode=%s", account_mode
        )
        return lock_token
    logger.info(
        "KIS approval key issuance lock contended: account_mode=%s", account_mode
    )
    return None


async def _release_issuance_lock(
    redis_client: redis.Redis, lock_key: str, lock_token: str
) -> None:
    """Release the issuance lock iff we still own it (compare-and-delete).

    Fail-open: a release error is swallowed because the lock TTL guarantees
    eventual recovery, and masking the holder's real error would be worse.
    """
    release_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    else
        return 0
    end
    """
    try:
        await redis_client.eval(release_script, 1, lock_key, lock_token)
    except Exception as exc:  # noqa: BLE001 - TTL self-heals; never mask holder error
        logger.debug("KIS approval key lock release best-effort failure: %s", exc)


async def _wait_for_cached_approval_key(account_mode: str) -> str | None:
    """Bounded, jittered wait for another holder to publish the cached key.

    Read-first: check the cache before each sleep so a key that is already
    present (holder cached quickly) is returned without burning a poll interval.
    """
    deadline = time.monotonic() + APPROVAL_KEY_WAIT_TIMEOUT_SECONDS
    while True:
        cached = await _get_cached_approval_key(account_mode)
        if _is_valid_approval_key(cached):
            return cached
        if time.monotonic() >= deadline:
            return None
        poll = max(float(APPROVAL_KEY_WAIT_POLL_SECONDS), 0.0)
        await asyncio.sleep(poll + random.uniform(0.0, poll))


async def _get_cached_approval_key(account_mode: str = "kis_live") -> str | None:
    """
    캐시된 Approval Key 조회 (account-mode 별 네임스페이스, 만료 체크 포함)

    Returns:
        str | None: 캐시된 Approval Key (없거나 만료된 경우 None)

    Raises:
        redis.RedisError: Redis 접근 실패 시 (엄격 실패 정책)
    """
    cache_key = APPROVAL_KEY_CACHE_KEYS[_resolve_ws_account_mode(account_mode)]
    redis_client = await _get_redis_client()
    cached_key = await redis_client.get(cache_key)
    return cached_key


async def _cache_approval_key(
    approval_key: str, account_mode: str = "kis_live"
) -> None:
    """
    Approval Key 캐싱 (account-mode 별 네임스페이스, 23시간 TTL)

    Args:
        approval_key: 캐싱할 Approval Key
        account_mode: "kis_live" (default) 또는 "kis_mock".

    Raises:
        redis.RedisError: Redis 접근 실패 시 (엄격 실패 정책)
    """
    cache_key = APPROVAL_KEY_CACHE_KEYS[_resolve_ws_account_mode(account_mode)]
    redis_client = await _get_redis_client()
    await redis_client.set(cache_key, approval_key, ex=APPROVAL_KEY_TTL_SECONDS)
    logger.info(
        "KIS Approval Key cached in Redis: account_mode=%s (TTL: 23h)", account_mode
    )


async def _invalidate_cached_approval_key(account_mode: str = "kis_live") -> None:
    """Delete the cached approval key (controlled invalidation only).

    Called solely from the lock-guarded reissue path so the deletion can never
    race a concurrent issuance. Never call this from a tight reconnect loop.

    Raises:
        redis.RedisError: Redis 접근 실패 시 (엄격 실패 정책)
    """
    cache_key = APPROVAL_KEY_CACHE_KEYS[_resolve_ws_account_mode(account_mode)]
    redis_client = await _get_redis_client()
    await redis_client.delete(cache_key)
    logger.info(
        "KIS approval key cache invalidated (controlled reissue): account_mode=%s",
        account_mode,
    )


async def _issue_approval_key(account_mode: str = "kis_live") -> str:
    """
    KIS Approval Key 발급 API 호출 (account-mode 별 endpoint/credential)

    Returns:
        str: 발급된 Approval Key

    Raises:
        ValueError: account_mode 미지원, mock 설정 누락, endpoint host 불일치 시
        Exception: HTTP 요청 실패 또는 응답에 approval_key 없음
    """
    account_mode = _resolve_ws_account_mode(account_mode)
    base_url, appkey, secret = _resolve_approval_credentials(account_mode)
    _assert_approval_endpoint_host(account_mode, base_url)

    url = f"{base_url}/oauth2/Approval"

    headers = {
        "Content-Type": "application/json",
    }

    request_body = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "secretkey": secret,
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

    logger.info("KIS Approval Key issued successfully: account_mode=%s", account_mode)
    return issued_key
