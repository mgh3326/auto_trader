"""Base KIS client with shared infrastructure for token management and rate limiting.

This module provides the foundation for all KIS sub-clients, handling:
- OAuth2 token management via Redis-backed cache
- Per-API rate limiting with sliding window
- HTTP request wrapper with 429 retry logic
"""

import asyncio
import inspect
import logging
import random
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

import httpx

from app.core.async_rate_limiter import RateLimitExceededError, get_limiter
from app.core.config import settings
from app.services.redis_token_manager import redis_token_manager


def _safe_parse_retry_after(value: str | None) -> float:
    """Safely parse Retry-After header, returning 0 on failure."""
    if not value:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _safe_status_code(response: object, *, default: int = 200) -> int:
    """Extract status code from response object safely."""
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else default


def _log_kis_api_failure(
    api_name: str,
    endpoint: str,
    tr_id: str,
    request_keys: list[str],
    msg_cd: str,
    msg1: str,
) -> None:
    """Log KIS API failure with structured context."""
    logging.error(
        "KIS API 실패: api_name=%s, endpoint=%s, tr_id=%s, request_keys=%s, msg_cd=%s, msg1=%s",
        api_name,
        endpoint,
        tr_id,
        sorted(request_keys),
        msg_cd,
        msg1,
    )
    if msg_cd == "OPSQ2001" or "CMA_EVLU_AMT_ICLD_YN" in str(msg1):
        logging.warning(
            "OPSQ2001/CMA_EVLU_AMT_ICLD_YN 감지: api_name=%s, endpoint=%s, tr_id=%s",
            api_name,
            endpoint,
            tr_id,
        )


class BaseKISClient:
    """Shared infrastructure for all KIS sub-clients.

    Provides:
    - Token management (fetch, cache, refresh)
    - Per-API rate limiting
    - HTTP request wrapper with retry logic

    Sub-clients should inherit from this class and use the protected
    methods for API calls.
    """

    _shared_http_client: ClassVar[httpx.AsyncClient | None] = None
    _shared_http_client_owner: ClassVar[object | None] = None
    _shared_http_client_entered: ClassVar[bool] = False
    _shared_client_lock: ClassVar[asyncio.Lock | None] = None
    _shared_http_client_builder_token: ClassVar[tuple[int, int] | None] = None

    def __init__(self) -> None:
        """Initialize base client with headers and token manager."""
        self._hdr_base = {
            "appkey": self._settings.kis_app_key,
            "appsecret": self._settings.kis_app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        self._token_manager = redis_token_manager
        self._unmapped_rate_limit_keys_logged: set[str] = set()
        if type(self)._shared_client_lock is None:
            type(self)._shared_client_lock = asyncio.Lock()

    @property
    def _http_client(self) -> httpx.AsyncClient | None:
        return type(self)._shared_http_client

    @_http_client.setter
    def _http_client(self, client: httpx.AsyncClient | None) -> None:
        type(self)._shared_http_client = client

    @property
    def _http_client_owner(self) -> object | None:
        return type(self)._shared_http_client_owner

    @_http_client_owner.setter
    def _http_client_owner(self, owner: object | None) -> None:
        type(self)._shared_http_client_owner = owner

    @property
    def _http_client_entered(self) -> bool:
        return type(self)._shared_http_client_entered

    @_http_client_entered.setter
    def _http_client_entered(self, entered: bool) -> None:
        type(self)._shared_http_client_entered = entered

    @property
    def _client_lock(self) -> asyncio.Lock:
        lock = type(self)._shared_client_lock
        if lock is None:
            lock = asyncio.Lock()
            type(self)._shared_client_lock = lock
        return lock

    @property
    def _settings(self):
        """Access to application settings."""
        return settings

    async def _get_limiter(self, api_key: str, *, rate: int, period: float) -> Any:
        return await get_limiter("kis", api_key, rate=rate, period=period)

    def _build_http_client(self, timeout: float) -> object:
        return httpx.AsyncClient(timeout=timeout)

    def _current_http_client_builder_token(self) -> tuple[int, int]:
        return (id(type(self)._build_http_client), id(httpx.AsyncClient))

    async def _open_http_client(self, timeout: float) -> httpx.AsyncClient:
        client_owner = self._build_http_client(timeout)
        self._http_client_owner = client_owner
        type(
            self
        )._shared_http_client_builder_token = self._current_http_client_builder_token()

        owner = cast(Any, client_owner)
        if callable(getattr(owner, "__aenter__", None)):
            entered = owner.__aenter__()
            if inspect.isawaitable(entered):
                client = cast(httpx.AsyncClient, await entered)
            else:
                client = cast(httpx.AsyncClient, entered)
            self._http_client = client
            self._http_client_entered = True
            return client

        client = cast(httpx.AsyncClient, client_owner)
        self._http_client = client
        self._http_client_entered = False
        return client

    async def _close_client_resources(
        self,
        *,
        client: httpx.AsyncClient | None,
        client_owner: object | None,
        client_entered: bool,
    ) -> None:
        if client_owner is not None and client_entered:
            aexit = getattr(client_owner, "__aexit__", None)
            if callable(aexit):
                result = aexit(None, None, None)
                if inspect.isawaitable(result):
                    await result
                logging.debug("KIS HTTP client closed")
                return

        if client is not None:
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                result = aclose()
                if inspect.isawaitable(result):
                    await result
            logging.debug("KIS HTTP client closed")

    async def _ensure_client(self, timeout: float | None = None) -> httpx.AsyncClient:
        """Get or create the persistent HTTP client (lazy initialization)."""
        current_builder_token = self._current_http_client_builder_token()
        client_to_close: httpx.AsyncClient | None = None
        owner_to_close: object | None = None
        entered_to_close = False

        async with self._client_lock:
            if (
                self._http_client is not None
                and type(self)._shared_http_client_builder_token is not None
                and type(self)._shared_http_client_builder_token
                != current_builder_token
            ):
                client_to_close = self._http_client
                owner_to_close = self._http_client_owner
                entered_to_close = self._http_client_entered
                self._http_client = None
                self._http_client_owner = None
                self._http_client_entered = False
                type(self)._shared_http_client_builder_token = None

        if client_to_close is not None or owner_to_close is not None:
            await self._close_client_resources(
                client=client_to_close,
                client_owner=owner_to_close,
                client_entered=entered_to_close,
            )

        if self._http_client is None:
            async with self._client_lock:
                if self._http_client is None:
                    _ = await self._open_http_client(timeout or 10.0)
        assert self._http_client is not None
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        async with self._client_lock:
            client = self._http_client
            client_owner = self._http_client_owner
            client_entered = self._http_client_entered

            self._http_client = None
            self._http_client_owner = None
            self._http_client_entered = False
            type(self)._shared_http_client_builder_token = None

        await self._close_client_resources(
            client=client,
            client_owner=client_owner,
            client_entered=client_entered,
        )

    async def _fetch_token(self) -> tuple[str, int]:
        """Fetch new OAuth2 token from KIS API.

        Returns:
            Tuple of (access_token, expires_in_seconds)

        Raises:
            httpx.HTTPStatusError: On HTTP errors
            KeyError: If response doesn't contain access_token
        """
        base_url = getattr(
            self._settings, "kis_base_url", "https://openapi.koreainvestment.com:9443"
        )
        cli = await self._ensure_client(timeout=5.0)
        r = await cli.post(
            f"{base_url}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "appkey": self._settings.kis_app_key,
                "appsecret": self._settings.kis_app_secret,
            },
            timeout=5,
        )
        response = r.json()
        access_token = response["access_token"]
        expires_in = response.get("expires_in", 3600)

        logging.info("KIS 새 토큰 발급 완료")
        return access_token, expires_in

    async def _ensure_token(self) -> None:
        """Ensure valid access token is available.

        Uses Redis-backed token manager for caching and distributed lock
        to prevent thundering herd on token refresh.
        """
        token = await self._token_manager.get_token()
        if token:
            self._settings.kis_access_token = token
            logging.debug("KIS access token ready for request")
            return

        async def token_fetcher() -> tuple[str, int]:
            access_token, expires_in = await self._fetch_token()
            return access_token, expires_in

        self._settings.kis_access_token = (
            await self._token_manager.refresh_token_with_lock(token_fetcher)
        )
        logging.info("KIS access token refreshed and applied")

    async def _request_with_rate_limit(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request with rate limiting and 429 retry logic.

        Args:
            method: HTTP method ("GET" or "POST")
            url: Full URL to request
            headers: Request headers (including authorization)
            params: Query parameters for GET requests
            json_body: JSON body for POST requests
            timeout: Request timeout in seconds
            api_name: Human-readable API name for logging
            tr_id: KIS TR_ID for per-API rate limiting

        Returns:
            Parsed JSON response

        Raises:
            RuntimeError: On KIS API errors after retries exhausted
            RateLimitExceededError: When rate limit retries exhausted
            httpx.HTTPStatusError: On HTTP errors after retries exhausted
        """
        parsed_url = urlparse(url)
        api_path = parsed_url.path or "/unknown"
        api_key = f"{tr_id or 'unknown'}|{api_path}"

        rate, period = self._get_rate_limit_for_api(api_key)
        limiter = await self._get_limiter(api_key, rate=rate, period=period)
        max_retries = self._settings.api_rate_limit_retry_429_max
        base_delay = self._settings.api_rate_limit_retry_429_base_delay

        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            await limiter.acquire(
                blocking_callback=lambda w: logging.warning(
                    "[%s] Rate limit wait: %.3fs (api=%s)",
                    "kis",
                    w,
                    api_name,
                )
            )

            try:
                client = await self._ensure_client(timeout=timeout)
                if method.upper() == "GET":
                    response = await client.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=timeout,
                    )
                else:
                    response = await client.post(
                        url,
                        headers=headers,
                        json=json_body,
                        timeout=timeout,
                    )
                status_code = _safe_status_code(response)

                if status_code == 429:
                    retry_after = _safe_parse_retry_after(
                        response.headers.get("Retry-After")
                    )
                    wait_time = (
                        retry_after
                        if retry_after > 0
                        else base_delay * (2**attempt) + random.uniform(0, 0.1)
                    )
                    logging.warning(
                        "[%s] 429 received for %s, attempt %d/%d, waiting %.3fs",
                        "kis",
                        api_name,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                try:
                    data = response.json()
                except ValueError as exc:
                    if status_code >= 400:
                        response.raise_for_status()
                    raise RuntimeError(
                        f"KIS API non-JSON response: {api_name}"
                    ) from exc

                if not isinstance(data, dict):
                    if status_code >= 400:
                        response.raise_for_status()
                    raise RuntimeError(f"KIS API non-JSON response: {api_name}")

                if status_code >= 400 and status_code != 500:
                    response.raise_for_status()

                rt_cd = data.get("rt_cd")
                msg_cd = str(data.get("msg_cd", ""))
                msg1 = str(data.get("msg1", ""))

                if rt_cd != "0":
                    rate_limit_heuristics = [
                        "RATE",
                        "LIMIT",
                        "요청제한",
                        "초과",
                    ]
                    is_rate_limit = any(
                        h in msg_cd.upper() or h in msg1.upper()
                        for h in rate_limit_heuristics
                    )

                    if is_rate_limit and attempt < max_retries:
                        wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                        logging.warning(
                            "[%s] Rate limit heuristic triggered for %s: %s %s, attempt %d/%d, waiting %.3fs",
                            "kis",
                            api_name,
                            msg_cd,
                            msg1,
                            attempt + 1,
                            max_retries + 1,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                return data

            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 429 and attempt < max_retries:
                    wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                    logging.warning(
                        "[%s] HTTP 429 for %s, attempt %d/%d, waiting %.3fs",
                        "kis",
                        api_name,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise
            except httpx.RequestError as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                    logging.warning(
                        "[%s] Request error for %s: %s, attempt %d/%d, retrying in %.3fs",
                        "kis",
                        api_name,
                        e,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise

        raise RateLimitExceededError(
            f"KIS rate limit retries exhausted for {api_name}: {last_error}"
        )

    def _get_rate_limit_for_api(self, api_key: str) -> tuple[int, float]:
        """Get rate limit for a specific API key, falling back to defaults.

        Args:
            api_key: API identifier in format "TR_ID|/path"

        Returns:
            Tuple of (rate, period) where rate is requests per period
        """

        def _safe_rate(value: Any, default: int) -> int:
            try:
                parsed = int(cast(Any, value))
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        def _safe_period(value: Any, default: float) -> float:
            try:
                parsed = float(cast(Any, value))
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        default_rate = _safe_rate(
            getattr(self._settings, "kis_rate_limit_rate", 19), 19
        )
        default_period = _safe_period(
            getattr(self._settings, "kis_rate_limit_period", 1.0), 1.0
        )

        api_limits = getattr(self._settings, "kis_api_rate_limits", {})
        if isinstance(api_limits, dict) and api_key in api_limits:
            limit_config = api_limits[api_key]
            if isinstance(limit_config, dict):
                rate = _safe_rate(limit_config.get("rate"), default_rate)
                period = _safe_period(limit_config.get("period"), default_period)
                return rate, period

        if api_key not in self._unmapped_rate_limit_keys_logged:
            logging.warning(
                "[kis] Unmapped API rate limit for %s, using defaults (%s/%ss)",
                api_key,
                default_rate,
                default_period,
            )
            self._unmapped_rate_limit_keys_logged.add(api_key)
        return default_rate, default_period

    async def _handle_token_expiry_and_retry(
        self,
        js: dict[str, Any],
        retry_func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Handle token expiry by clearing cache and retrying.

        Args:
            js: API response JSON
            retry_func: Async function to retry
            *args: Positional args for retry_func
            **kwargs: Keyword args for retry_func

        Returns:
            Result from retry_func

        Raises:
            RuntimeError: If not a token expiry error
        """
        msg_cd = js.get("msg_cd", "")
        if msg_cd in ("EGW00123", "EGW00121"):
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await retry_func(*args, **kwargs)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )
