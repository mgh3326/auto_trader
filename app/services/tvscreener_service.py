"""TvScreener service wrapper with async support, retry logic, and error handling.

This module provides async wrappers for the tvscreener library (which is synchronous)
and implements robust error handling, rate limiting, and field discovery utilities.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class TvScreenerError(Exception):
    """Base exception for TvScreener service errors."""

    pass


class TvScreenerRateLimitError(TvScreenerError):
    """Raised when TradingView API rate limit is exceeded."""

    pass


class TvScreenerMalformedRequestError(TvScreenerError):
    """Raised when TradingView rejects a malformed request."""

    pass


class TvScreenerTimeoutError(TvScreenerError):
    """Raised when a TvScreener request times out."""

    pass


class TvScreenerService:
    """Async wrapper service for tvscreener library with error handling and retry logic.

    This service wraps the synchronous tvscreener library to provide:
    - Async API using asyncio.to_thread()
    - Automatic retry with exponential backoff on rate limits
    - Field discovery to verify available fields
    - Comprehensive error handling and logging

    Attributes
    ----------
    max_retries : int
        Maximum number of retry attempts for API calls
    base_delay : float
        Base delay in seconds for exponential backoff
    timeout : float
        Timeout in seconds for API calls
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        timeout: float = 30.0,
    ) -> None:
        """Initialize TvScreener service.

        Parameters
        ----------
        max_retries : int, optional
            Maximum number of retry attempts, by default 3
        base_delay : float, optional
            Base delay for exponential backoff in seconds, by default 1.0
        timeout : float, optional
            Request timeout in seconds, by default 30.0
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = timeout
        self._field_cache: dict[str, list[tuple[str, Any]]] = {}
        logger.info(
            "TvScreenerService initialized with max_retries=%d, base_delay=%.1fs, timeout=%.1fs",
            max_retries,
            base_delay,
            timeout,
        )

    async def fetch_with_retry(
        self,
        screener_callable: Callable[[], pd.DataFrame],
        operation_name: str = "screener_query",
    ) -> pd.DataFrame:
        """Execute a tvscreener query with retry logic and exponential backoff.

        Parameters
        ----------
        screener_callable : Callable[[], pd.DataFrame]
            Synchronous callable that returns a pandas DataFrame from tvscreener
        operation_name : str, optional
            Name of the operation for logging, by default "screener_query"

        Returns
        -------
        pd.DataFrame
            Results from the tvscreener query

        Raises
        ------
        TvScreenerRateLimitError
            If rate limit is exceeded after all retries
        TvScreenerMalformedRequestError
            If request is malformed and retries are exhausted
        TvScreenerTimeoutError
            If request times out
        TvScreenerError
            For other unexpected errors

        Notes
        -----
        - Uses asyncio.to_thread() to wrap synchronous tvscreener calls
        - Implements exponential backoff: delay = base_delay * (2 ** attempt)
        - Logs all errors and retry attempts for debugging
        """
        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                logger.debug(
                    "Executing %s (attempt %d/%d)",
                    operation_name,
                    attempt + 1,
                    self.max_retries,
                )

                # Execute synchronous tvscreener call in thread pool
                result = await asyncio.wait_for(
                    asyncio.to_thread(screener_callable),
                    timeout=self.timeout,
                )

                elapsed = time.time() - start_time
                logger.info(
                    "%s completed successfully in %.2fs (attempt %d/%d)",
                    operation_name,
                    elapsed,
                    attempt + 1,
                    self.max_retries,
                )

                return result

            except TimeoutError as exc:
                elapsed = time.time() - start_time
                logger.error(
                    "%s timed out after %.2fs (attempt %d/%d)",
                    operation_name,
                    elapsed,
                    attempt + 1,
                    self.max_retries,
                )
                if attempt < self.max_retries - 1:
                    delay = self.base_delay * (2**attempt)
                    logger.info("Retrying after %.2fs delay...", delay)
                    await asyncio.sleep(delay)
                    continue
                raise TvScreenerTimeoutError(
                    f"{operation_name} timed out after {self.timeout}s"
                ) from exc

            except Exception as exc:
                # Try to identify specific error types from tvscreener
                exc_type = type(exc).__name__
                exc_msg = str(exc)

                # Check for rate limiting indicators
                if "malformed" in exc_msg.lower() or exc_type == "MalformedRequestException":
                    logger.warning(
                        "%s received malformed request error (attempt %d/%d): %s: %s",
                        operation_name,
                        attempt + 1,
                        self.max_retries,
                        exc_type,
                        exc_msg,
                    )
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2**attempt)
                        logger.info(
                            "Rate limit suspected, retrying after %.2fs delay...", delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise TvScreenerMalformedRequestError(
                        f"{operation_name} failed with malformed request: {exc_msg}"
                    ) from exc

                # Check for explicit rate limit errors
                if "rate limit" in exc_msg.lower() or "too many requests" in exc_msg.lower():
                    logger.warning(
                        "%s hit rate limit (attempt %d/%d): %s",
                        operation_name,
                        attempt + 1,
                        self.max_retries,
                        exc_msg,
                    )
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2**attempt)
                        logger.info("Retrying after %.2fs delay...", delay)
                        await asyncio.sleep(delay)
                        continue
                    raise TvScreenerRateLimitError(
                        f"{operation_name} exceeded rate limit: {exc_msg}"
                    ) from exc

                # Unexpected error - log and raise
                logger.error(
                    "%s failed with unexpected error (attempt %d/%d): %s: %s",
                    operation_name,
                    attempt + 1,
                    self.max_retries,
                    exc_type,
                    exc_msg,
                    exc_info=True,
                )
                if attempt < self.max_retries - 1:
                    delay = self.base_delay * (2**attempt)
                    logger.info("Retrying after %.2fs delay...", delay)
                    await asyncio.sleep(delay)
                    continue
                raise TvScreenerError(
                    f"{operation_name} failed: {exc_type}: {exc_msg}"
                ) from exc

        raise TvScreenerError(
            f"{operation_name} failed after {self.max_retries} attempts"
        )

    async def discover_fields(
        self, screener_class: type, field_enum: type
    ) -> list[tuple[str, Any]]:
        """Discover which fields are available for a screener type.

        This utility inspects the field enum to determine which fields are actually
        available, which is important because some fields (like ADX for crypto)
        may not be available despite being in the enum.

        Parameters
        ----------
        screener_class : type
            The screener class (e.g., CryptoScreener, StockScreener)
        field_enum : type
            The field enum class (e.g., CryptoField, StockField)

        Returns
        -------
        list[tuple[str, Any]]
            List of (field_name, field_value) tuples for available fields

        Notes
        -----
        Results are cached to avoid repeated introspection.
        """
        cache_key = f"{screener_class.__name__}:{field_enum.__name__}"

        # Return cached results if available
        if cache_key in self._field_cache:
            logger.debug(
                "Returning cached field discovery for %s", cache_key
            )
            return self._field_cache[cache_key]

        logger.info(
            "Discovering available fields for %s with %s",
            screener_class.__name__,
            field_enum.__name__,
        )

        available_fields: list[tuple[str, Any]] = []

        def _discover() -> list[tuple[str, Any]]:
            """Synchronous field discovery."""
            fields: list[tuple[str, Any]] = []
            for field_name in dir(field_enum):
                # Skip private/magic methods
                if field_name.startswith("_"):
                    continue
                try:
                    field = getattr(field_enum, field_name)
                    # Filter out methods and other non-field attributes
                    if callable(field):
                        continue
                    fields.append((field_name, field))
                except Exception as exc:
                    logger.debug(
                        "Failed to access field %s.%s: %s",
                        field_enum.__name__,
                        field_name,
                        exc,
                    )
            return fields

        try:
            available_fields = await asyncio.to_thread(_discover)
            self._field_cache[cache_key] = available_fields

            logger.info(
                "Discovered %d fields for %s: %s",
                len(available_fields),
                cache_key,
                ", ".join(f[0] for f in available_fields[:10])
                + ("..." if len(available_fields) > 10 else ""),
            )

        except Exception as exc:
            logger.error(
                "Field discovery failed for %s: %s: %s",
                cache_key,
                type(exc).__name__,
                exc,
            )
            # Return empty list on failure - caller should handle gracefully
            return []

        return available_fields

    async def query_crypto_screener(
        self,
        columns: list[Any],
        where_clause: Any | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Query CryptoScreener with specified columns and filters.

        Parameters
        ----------
        columns : list[Any]
            List of CryptoField enums to retrieve
        where_clause : Any | None, optional
            WHERE clause for filtering (e.g., CryptoField.RSI_14 < 30)
        sort_by : str | None, optional
            Column name to sort by
        limit : int | None, optional
            Maximum number of results

        Returns
        -------
        pd.DataFrame
            Query results

        Raises
        ------
        TvScreenerError
            If the query fails
        """
        try:
            # Import here to avoid import errors if tvscreener not installed
            from tvscreener import CryptoScreener

            def _execute_query() -> pd.DataFrame:
                """Synchronous query execution."""
                screener = CryptoScreener()
                query = screener.select(*columns)

                if where_clause is not None:
                    query = query.where(where_clause)

                if sort_by:
                    query = query.order_by(sort_by)

                if limit:
                    query = query.limit(limit)

                return query.get()

            result = await self.fetch_with_retry(
                _execute_query,
                operation_name=f"CryptoScreener query (columns={len(columns)})",
            )

            logger.info(
                "CryptoScreener query returned %d rows, %d columns",
                len(result),
                len(result.columns),
            )

            return result

        except ImportError as exc:
            raise TvScreenerError(
                "tvscreener library not installed. Install with: pip install tvscreener"
            ) from exc

    async def query_stock_screener(
        self,
        columns: list[Any],
        where_clause: Any | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
        country: str | None = None,
    ) -> pd.DataFrame:
        """Query StockScreener with specified columns and filters.

        Parameters
        ----------
        columns : list[Any]
            List of StockField enums to retrieve
        where_clause : Any | None, optional
            WHERE clause for filtering
        sort_by : str | None, optional
            Column name to sort by
        limit : int | None, optional
            Maximum number of results
        country : str | None, optional
            Country filter (e.g., "South Korea", "United States")

        Returns
        -------
        pd.DataFrame
            Query results

        Raises
        ------
        TvScreenerError
            If the query fails
        """
        try:
            # Import here to avoid import errors if tvscreener not installed
            from tvscreener import StockField, StockScreener

            def _execute_query() -> pd.DataFrame:
                """Synchronous query execution."""
                screener = StockScreener()
                query = screener.select(*columns)

                # Apply country filter if specified
                if country:
                    country_filter = StockField.COUNTRY == country
                    if where_clause is not None:
                        query = query.where(country_filter & where_clause)
                    else:
                        query = query.where(country_filter)
                elif where_clause is not None:
                    query = query.where(where_clause)

                if sort_by:
                    query = query.order_by(sort_by)

                if limit:
                    query = query.limit(limit)

                return query.get()

            result = await self.fetch_with_retry(
                _execute_query,
                operation_name=f"StockScreener query (country={country}, columns={len(columns)})",
            )

            logger.info(
                "StockScreener query returned %d rows, %d columns",
                len(result),
                len(result.columns),
            )

            return result

        except ImportError as exc:
            raise TvScreenerError(
                "tvscreener library not installed. Install with: pip install tvscreener"
            ) from exc


# Singleton instance for convenience
_default_service: TvScreenerService | None = None


def get_tvscreener_service() -> TvScreenerService:
    """Get or create the default TvScreener service instance.

    Returns
    -------
    TvScreenerService
        Singleton service instance
    """
    global _default_service
    if _default_service is None:
        _default_service = TvScreenerService()
    return _default_service


__all__ = [
    "TvScreenerService",
    "TvScreenerError",
    "TvScreenerRateLimitError",
    "TvScreenerMalformedRequestError",
    "TvScreenerTimeoutError",
    "get_tvscreener_service",
]
