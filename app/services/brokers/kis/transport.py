"""KIS API transport layer with rate limiting, retry logic, and token management.

This module provides the KISTransport class that handles all HTTP communication
with the KIS (Korea Investment & Securities) API, including:
- Rate limiting per API endpoint
- Automatic token refresh on expiry
- Retry logic with exponential backoff for 429 errors
- Logging of API failures

The transport layer is designed to be used as a dependency by other API modules
(HoldingsAPI, OrdersAPI, MarketDataAPI) via constructor injection.
"""

import logging
from typing import TYPE_CHECKING, Any

import httpx
import pandas as pd

from app.core.config import settings
from app.services.redis_token_manager import redis_token_manager

if TYPE_CHECKING:
    pass  # Forward declarations for type hints

# ============================================================================
# KIS API BASE URL
# ============================================================================

BASE = "https://openapi.koreainvestment.com:9443"

# ============================================================================
# DATA CONSTANTS (for chart data validation)
# ============================================================================

_DAY_FRAME_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]
_DAILY_ITEMCHARTPRICE_REQUIRED_FIELDS = {
    "stck_bsop_date",
    "stck_oprc",
    "stck_hgpr",
    "stck_lwpr",
    "stck_clpr",
    "acml_vol",
    "acml_tr_pbmn",
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def _safe_parse_retry_after(value: str | None) -> float:
    """Safely parse Retry-After header, returning 0 on failure.

    Args:
        value: The Retry-After header value (can be seconds or HTTP date)

    Returns:
        Parsed float value, or 0.0 if parsing fails
    """
    if not value:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _safe_status_code(response: object, *, default: int = 200) -> int:
    """Safely extract status code from HTTP response.

    Args:
        response: HTTP response object (httpx.Response or similar)
        default: Default value if status_code attribute not found

    Returns:
        Integer status code, or default if extraction fails
    """
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else default


def _empty_day_frame() -> pd.DataFrame:
    """Create an empty DataFrame with the standard day frame columns.

    Returns:
        Empty DataFrame with columns: date, open, high, low, close, volume, value
    """
    return pd.DataFrame(columns=_DAY_FRAME_COLUMNS)


def _validate_daily_itemchartprice_chunk(chunk: list[dict[str, Any]]) -> None:
    """Validate a chunk of daily chart data from KIS API.

    Args:
        chunk: List of dictionaries containing daily chart data

    Raises:
        RuntimeError: If the chunk is malformed or missing required fields
    """
    if not isinstance(chunk, list):
        raise RuntimeError(
            "Malformed KIS daily chart payload: expected list in output2/output"
        )

    for index, row in enumerate(chunk):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Malformed KIS daily chart payload at row {index}: expected object"
            )

        missing = sorted(
            field
            for field in _DAILY_ITEMCHARTPRICE_REQUIRED_FIELDS
            if row.get(field) is None or row.get(field) == ""
        )
        if missing:
            missing_fields = ", ".join(missing)
            raise RuntimeError(
                f"Malformed KIS daily chart payload at row {index}: missing {missing_fields}"
            )


def _log_kis_api_failure(
    api_name: str,
    endpoint: str,
    tr_id: str,
    request_keys: list[str],
    msg_cd: str,
    msg1: str,
) -> None:
    """Log KIS API failure with structured information.

    Note: Only key names are logged, never values, to protect sensitive data.

    Args:
        api_name: Human-readable API name for logging
        endpoint: API endpoint path
        tr_id: Transaction ID
        request_keys: List of request parameter keys (values never logged)
        msg_cd: KIS error message code
        msg1: KIS error message text
    """
    # Log all key names for debugging (OPSQ2001 diagnosis requires visibility)
    # Values are never logged - only key names
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


# ============================================================================
# TRANSPORT CLASS
# ============================================================================


class KISTransport:
    """HTTP transport layer for KIS API communication.

    This class handles:
    - HTTP requests to KIS API endpoints
    - Rate limiting per API endpoint
    - Automatic token refresh on expiry (EGW00123/EGW00121 errors)
    - Retry logic with exponential backoff for 429 errors
    - Logging of API failures

    The transport layer uses redis_token_manager for token lifecycle management
    and async_rate_limiter for per-endpoint rate limiting.

    Example:
        transport = KISTransport()
        response = await transport.request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={"authorization": "Bearer token"},
            params={"FID_INPUT_ISCD": "005930"},
            api_name="inquire_price",
            tr_id="FHKST01010100",
        )
    """

    def __init__(self) -> None:
        """Initialize the KIS transport layer."""
        # Token manager via composition (shared instance)
        self._token_manager = redis_token_manager

        # Track unmapped rate limit keys that have been logged (to avoid spam)
        self._unmapped_rate_limit_keys_logged: set[str] = set()

    async def request(
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
        """Make an HTTP request to KIS API with rate limiting and retry logic.

        This method handles:
        - Rate limiting before the request
        - Token expiry errors (EGW00123/EGW00121) with automatic refresh
        - 429 rate limit errors with exponential backoff retry

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full URL or path to the API endpoint
            headers: Request headers (including authorization)
            params: Query parameters
            json_body: JSON body for POST requests
            timeout: Request timeout in seconds
            api_name: Human-readable API name for logging
            tr_id: KIS transaction ID for rate limiting lookup

        Returns:
            JSON response as dictionary

        Raises:
            httpx.HTTPStatusError: For non-retryable HTTP errors
            RuntimeError: For API-level errors
        """
        # TODO: Implement in subtask-1-3
        # - Rate limiting via get_limiter()
        # - HTTP request via httpx
        # - Token expiry handling (EGW00123/EGW00121)
        # - 429 retry with exponential backoff
        raise NotImplementedError("Transport.request() will be implemented in subtask-1-3")

    async def ensure_token(self) -> str:
        """Ensure a valid access token exists, refreshing if necessary.

        This method checks the token validity and refreshes it if:
        - No token exists
        - Current token is expired or about to expire

        Returns:
            Valid access token string

        Raises:
            RuntimeError: If token fetch fails after retries
        """
        # First check if we have a valid token in cache
        token = await self._token_manager.get_token()
        if token:
            logging.debug("KIS access token ready from cache")
            return token

        # Token is missing or expired - refresh with distributed lock
        async def token_fetcher() -> tuple[str, int]:
            return await self._fetch_token_with_expiry()

        token = await self._token_manager.refresh_token_with_lock(token_fetcher)
        logging.info("KIS access token refreshed and ready")
        return token

    async def _fetch_token_with_expiry(self) -> tuple[str, int]:
        """Fetch a new access token from KIS API with expiry information.

        This is an internal method that makes the actual HTTP call to
        obtain a fresh token. It should only be called via ensure_token()
        through the token manager's refresh_token_with_lock method.

        Returns:
            Tuple of (access_token, expires_in_seconds)

        Raises:
            RuntimeError: If token fetch fails
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "appkey": settings.kis_app_key,
                    "appsecret": settings.kis_app_secret,
                },
                timeout=5.0,
            )

        response_data = response.json()
        access_token = response_data["access_token"]
        expires_in = response_data.get("expires_in", 3600)  # Default 1 hour

        logging.info("KIS new token fetched successfully")
        return access_token, expires_in

    async def _fetch_token(self) -> str:
        """Fetch a new access token from KIS API.

        This is an internal method that makes the actual HTTP call to
        obtain a fresh token. It should only be called by ensure_token().

        Returns:
            New access token string

        Raises:
            RuntimeError: If token fetch fails
        """
        token, _ = await self._fetch_token_with_expiry()
        return token

    def _get_rate_limit_for_api(self, api_name: str, tr_id: str | None) -> tuple[int, int]:
        """Get rate limit configuration for a specific API.

        Rate limits vary by API endpoint. This method maps API names and
        transaction IDs to their corresponding rate limit configurations.

        Args:
            api_name: Human-readable API name
            tr_id: KIS transaction ID

        Returns:
            Tuple of (requests_per_second, burst_size) for rate limiting
        """
        # TODO: Implement in subtask-1-3
        # - Map API names to rate limit configs
        # - Default to conservative limits if unknown
        return (1, 1)  # Default: 1 request per second, no burst
