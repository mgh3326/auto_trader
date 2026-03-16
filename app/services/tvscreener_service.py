"""TvScreener service wrapper with async support, retry logic, and error handling.

This module provides async wrappers for the tvscreener library (which is synchronous)
and implements robust error handling, rate limiting, and field discovery utilities.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_COLUMN_NAME_MAP = {
    "symbol": "symbol",
    "name": "name",
    "description": "description",
    "active symbol": "active_symbol",
    "relative strength index (14)": "relative_strength_index_14",
    "average directional index (14)": "average_directional_index_14",
    "volume": "volume",
    "volume 24h in usd": "volume_24h_in_usd",
    "change %": "change_percent",
    "market capitalization": "market_capitalization",
    "market cap basic": "market_capitalization",
    "price to earnings ratio (ttm)": "price_to_earnings_ratio_ttm",
    "price to earnings ttm": "price_to_earnings_ratio_ttm",
    "price to book (fq)": "price_to_book_fq",
    "dividend yield forward": "dividend_yield_forward",
    "dividend yield recent": "dividend_yield_forward",
    "country": "country",
    "exchange": "exchange",
}


def _import_tvscreener() -> Any:
    return importlib.import_module("tvscreener")


def _normalize_column_name(column_name: Any) -> str:
    text = str(column_name or "").strip()
    if not text:
        return ""

    mapped = _COLUMN_NAME_MAP.get(text.lower())
    if mapped is not None:
        return mapped

    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return normalized


def _normalize_result_frame(result: pd.DataFrame) -> pd.DataFrame:
    if result.empty and len(result.columns) == 0:
        return result

    renamed = {column: _normalize_column_name(column) for column in result.columns}
    return result.rename(columns=renamed)


def _normalize_where_clauses(where_clause: Any | None) -> list[Any]:
    if where_clause is None:
        return []
    if isinstance(where_clause, Iterable) and not isinstance(
        where_clause, (str, bytes)
    ):
        return [condition for condition in where_clause if condition is not None]
    return [where_clause]


def _has_probe_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        return not bool(pd.isna(value))
    except TypeError:
        return True


def _evaluate_stock_probe_result(
    *,
    capability_name: str,
    field: object,
    result: pd.DataFrame,
) -> TvScreenerCapabilityState:
    if result.empty:
        return TvScreenerCapabilityState.UNKNOWN

    expected_columns = {
        _normalize_column_name(field),
        *(
            _normalize_column_name(alias)
            for alias in _STOCK_CAPABILITY_ALIASES.get(capability_name, ())
        ),
    }
    expected_columns.discard("")

    matching_columns = [
        column
        for column in result.columns
        if _normalize_column_name(column) in expected_columns
    ]
    if not matching_columns:
        return TvScreenerCapabilityState.UNKNOWN

    for column in matching_columns:
        series = result[column]
        if any(_has_probe_value(value) for value in series.tolist()):
            return TvScreenerCapabilityState.USABLE

    return TvScreenerCapabilityState.UNKNOWN


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


class TvScreenerCapabilityState(StrEnum):
    USABLE = "usable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TvScreenerCapabilitySnapshot:
    screener: str
    market: str
    statuses: dict[str, TvScreenerCapabilityState]
    fields: dict[str, object | None]

    def status(self, capability_name: str) -> TvScreenerCapabilityState:
        return self.statuses.get(capability_name, TvScreenerCapabilityState.UNKNOWN)

    def field(self, capability_name: str) -> object | None:
        return self.fields.get(capability_name)

    def is_usable(self, capability_name: str) -> bool:
        return (
            self.status(capability_name) is TvScreenerCapabilityState.USABLE
            and self.field(capability_name) is not None
        )


_STOCK_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("NAME",),
    "price": ("PRICE",),
    "rsi": ("RELATIVE_STRENGTH_INDEX_14",),
    "adx": ("AVERAGE_DIRECTIONAL_INDEX_14",),
    "volume": ("VOLUME",),
    "change_rate": ("CHANGE_PERCENT",),
    "market_cap": ("MARKET_CAPITALIZATION", "MARKET_CAP_BASIC"),
    "pe": ("PRICE_TO_EARNINGS_RATIO_TTM", "PRICE_TO_EARNINGS_TTM"),
    "pbr": ("PRICE_TO_BOOK_FQ",),
    "dividend_yield": ("DIVIDEND_YIELD_FORWARD", "DIVIDEND_YIELD_RECENT"),
    "sector": ("SECTOR",),
}

_CRYPTO_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("NAME",),
    "description": ("DESCRIPTION",),
    "price": ("PRICE",),
    "rsi": ("RELATIVE_STRENGTH_INDEX_14",),
    "adx": ("AVERAGE_DIRECTIONAL_INDEX_14",),
    "value_traded": ("VALUE_TRADED",),
    "market_cap": ("MARKET_CAP",),
}

_CAPABILITY_CACHE_MISS = object()


class _TvScreenerCapabilityRegistry:
    def __init__(self) -> None:
        self._field_cache: dict[tuple[str, str], object | None] = {}
        self._status_cache: dict[tuple[str, str, str], TvScreenerCapabilityState] = {}
        self._probe_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def get_field(self, screener: str, capability_name: str) -> object:
        return self._field_cache.get(
            (screener, capability_name),
            _CAPABILITY_CACHE_MISS,
        )

    def set_field(
        self,
        screener: str,
        capability_name: str,
        field: object | None,
    ) -> None:
        self._field_cache[(screener, capability_name)] = field

    def get_status(
        self,
        screener: str,
        market: str,
        capability_name: str,
    ) -> TvScreenerCapabilityState | None:
        return self._status_cache.get((screener, market, capability_name))

    def set_status(
        self,
        screener: str,
        market: str,
        capability_name: str,
        status: TvScreenerCapabilityState,
    ) -> None:
        self._status_cache[(screener, market, capability_name)] = status

    def get_probe_lock(
        self,
        screener: str,
        market: str,
        capability_name: str,
    ) -> asyncio.Lock:
        cache_key = (screener, market, capability_name)
        lock = self._probe_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._probe_locks[cache_key] = lock
        return lock


_shared_capability_registry = _TvScreenerCapabilityRegistry()
_STOCK_CAPABILITY_PROBE_LIMIT = 3


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
        capability_registry: _TvScreenerCapabilityRegistry | None = None,
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
        self._capability_registry = capability_registry or _shared_capability_registry
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
            start_time = time.time()
            try:
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
                if (
                    "malformed" in exc_msg.lower()
                    or exc_type == "MalformedRequestException"
                ):
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
                if (
                    "rate limit" in exc_msg.lower()
                    or "too many requests" in exc_msg.lower()
                ):
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
            logger.debug("Returning cached field discovery for %s", cache_key)
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

    @staticmethod
    def _normalize_stock_market(market: str) -> str:
        normalized = str(market or "").strip().lower()
        if normalized in {"kr", "kospi", "kosdaq", "korea"}:
            return "kr"
        if normalized in {"us", "america", "united states"}:
            return "us"
        return normalized

    async def _resolve_capability_fields(
        self,
        *,
        screener: str,
        screener_class: type,
        field_enum: type,
        capability_aliases: dict[str, tuple[str, ...]],
        capability_names: set[str],
    ) -> dict[str, object | None]:
        available_fields = dict(await self.discover_fields(screener_class, field_enum))
        resolved_fields: dict[str, object | None] = {}

        for capability_name in capability_names:
            cached_field = self._capability_registry.get_field(
                screener, capability_name
            )
            if cached_field is _CAPABILITY_CACHE_MISS:
                resolved_field: object | None = None
                for alias in capability_aliases.get(capability_name, ()):
                    if alias in available_fields:
                        resolved_field = available_fields[alias]
                        break
                    field_value = getattr(field_enum, alias, None)
                    if field_value is not None and not callable(field_value):
                        resolved_field = field_value
                        break
                self._capability_registry.set_field(
                    screener,
                    capability_name,
                    resolved_field,
                )
                cached_field = resolved_field

            resolved_fields[capability_name] = (
                None if cached_field is _CAPABILITY_CACHE_MISS else cached_field
            )

        return resolved_fields

    async def _probe_stock_capability(
        self,
        *,
        market: str,
        capability_name: str,
        field: object,
    ) -> TvScreenerCapabilityState:
        normalized_market = self._normalize_stock_market(market)
        cached_status = self._capability_registry.get_status(
            "stock",
            normalized_market,
            capability_name,
        )
        if cached_status is not None:
            return cached_status

        probe_lock = self._capability_registry.get_probe_lock(
            "stock",
            normalized_market,
            capability_name,
        )

        async with probe_lock:
            cached_status = self._capability_registry.get_status(
                "stock",
                normalized_market,
                capability_name,
            )
            if cached_status is not None:
                return cached_status

            try:
                tvscreener = _import_tvscreener()
                StockField = tvscreener.StockField
                Market = tvscreener.Market
            except ImportError:
                return TvScreenerCapabilityState.UNKNOWN

            probe_columns = [StockField.NAME]
            if field != StockField.NAME:
                probe_columns.append(field)

            probe_markets = None
            probe_country = None
            if normalized_market == "kr":
                probe_markets = [Market.KOREA]
            elif normalized_market == "us":
                probe_markets = [Market.AMERICA]
                probe_country = "United States"
            else:
                return TvScreenerCapabilityState.UNKNOWN

            try:
                probe_result = await self.query_stock_screener(
                    columns=probe_columns,
                    where_clause=None,
                    country=probe_country,
                    markets=probe_markets,
                    limit=_STOCK_CAPABILITY_PROBE_LIMIT,
                )
            except TvScreenerMalformedRequestError:
                status = TvScreenerCapabilityState.UNSUPPORTED
            except (
                TvScreenerError,
                TvScreenerRateLimitError,
                TvScreenerTimeoutError,
            ):
                return TvScreenerCapabilityState.UNKNOWN
            else:
                status = _evaluate_stock_probe_result(
                    capability_name=capability_name,
                    field=field,
                    result=probe_result,
                )

            self._capability_registry.set_status(
                "stock",
                normalized_market,
                capability_name,
                status,
            )
            return status

    async def get_stock_capabilities(
        self,
        *,
        market: str,
        capability_names: Iterable[str],
    ) -> TvScreenerCapabilitySnapshot:
        requested_capabilities = {
            str(capability_name).strip()
            for capability_name in capability_names
            if str(capability_name).strip()
        }
        normalized_market = self._normalize_stock_market(market)

        if not requested_capabilities:
            return TvScreenerCapabilitySnapshot(
                screener="stock",
                market=normalized_market,
                statuses={},
                fields={},
            )

        try:
            tvscreener = _import_tvscreener()
        except ImportError:
            return TvScreenerCapabilitySnapshot(
                screener="stock",
                market=normalized_market,
                statuses=dict.fromkeys(
                    requested_capabilities, TvScreenerCapabilityState.UNKNOWN
                ),
                fields=dict.fromkeys(requested_capabilities),
            )

        resolved_fields = await self._resolve_capability_fields(
            screener="stock",
            screener_class=tvscreener.StockScreener,
            field_enum=tvscreener.StockField,
            capability_aliases=_STOCK_CAPABILITY_ALIASES,
            capability_names=requested_capabilities,
        )

        capability_fields: dict[str, object | None] = {}
        capability_statuses: dict[str, TvScreenerCapabilityState] = {}

        for capability_name in requested_capabilities:
            resolved_field = resolved_fields.get(capability_name)
            capability_fields[capability_name] = resolved_field

            if resolved_field is None:
                self._capability_registry.set_status(
                    "stock",
                    normalized_market,
                    capability_name,
                    TvScreenerCapabilityState.UNSUPPORTED,
                )
                capability_statuses[capability_name] = (
                    TvScreenerCapabilityState.UNSUPPORTED
                )
                continue

            cached_status = self._capability_registry.get_status(
                "stock",
                normalized_market,
                capability_name,
            )
            if cached_status is not None:
                capability_statuses[capability_name] = cached_status
                continue

            capability_statuses[capability_name] = await self._probe_stock_capability(
                market=normalized_market,
                capability_name=capability_name,
                field=resolved_field,
            )

        return TvScreenerCapabilitySnapshot(
            screener="stock",
            market=normalized_market,
            statuses=capability_statuses,
            fields=capability_fields,
        )

    async def get_crypto_capabilities(
        self,
        capability_names: Iterable[str],
    ) -> TvScreenerCapabilitySnapshot:
        requested_capabilities = {
            str(capability_name).strip()
            for capability_name in capability_names
            if str(capability_name).strip()
        }

        if not requested_capabilities:
            return TvScreenerCapabilitySnapshot(
                screener="crypto",
                market="crypto",
                statuses={},
                fields={},
            )

        try:
            tvscreener = _import_tvscreener()
        except ImportError:
            return TvScreenerCapabilitySnapshot(
                screener="crypto",
                market="crypto",
                statuses=dict.fromkeys(
                    requested_capabilities, TvScreenerCapabilityState.UNKNOWN
                ),
                fields=dict.fromkeys(requested_capabilities),
            )

        resolved_fields = await self._resolve_capability_fields(
            screener="crypto",
            screener_class=tvscreener.CryptoScreener,
            field_enum=tvscreener.CryptoField,
            capability_aliases=_CRYPTO_CAPABILITY_ALIASES,
            capability_names=requested_capabilities,
        )

        capability_statuses = {
            capability_name: (
                TvScreenerCapabilityState.USABLE
                if resolved_fields.get(capability_name) is not None
                else TvScreenerCapabilityState.UNSUPPORTED
            )
            for capability_name in requested_capabilities
        }

        return TvScreenerCapabilitySnapshot(
            screener="crypto",
            market="crypto",
            statuses=capability_statuses,
            fields=resolved_fields,
        )

    async def query_crypto_screener(
        self,
        columns: list[Any],
        where_clause: Any | None = None,
        sort_by: Any | None = None,
        ascending: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Query CryptoScreener with specified columns and filters.

        Parameters
        ----------
        columns : list[Any]
            List of CryptoField enums to retrieve
        where_clause : Any | None, optional
            WHERE clause or list of WHERE clauses for filtering
        sort_by : Any | None, optional
            Field enum to sort by (e.g., CryptoField.RELATIVE_STRENGTH_INDEX_14)
        ascending : bool, optional
            Sort direction (True for ascending, False for descending), by default True
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
            tvscreener = _import_tvscreener()
            CryptoScreener = tvscreener.CryptoScreener
            where_clauses = _normalize_where_clauses(where_clause)

            def _execute_query() -> pd.DataFrame:
                screener = CryptoScreener()
                query = screener.select(*columns)
                if query is None:
                    raise TvScreenerError(
                        "CryptoScreener.select() returned None. "
                        f"Columns: {columns[:3]}... (total {len(columns)})"
                    )

                for condition in where_clauses:
                    query = query.where(condition)

                if sort_by:
                    query = query.sort_by(sort_by, ascending=ascending)

                if limit:
                    query = query.set_range(0, limit)

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

            return _normalize_result_frame(result)

        except ImportError as exc:
            raise TvScreenerError(
                "tvscreener library not installed. Install with: pip install tvscreener"
            ) from exc

    async def query_stock_screener(
        self,
        columns: list[Any],
        where_clause: Any | None = None,
        sort_by: Any | None = None,
        ascending: bool = True,
        limit: int | None = None,
        country: str | None = None,
        markets: list[Any] | tuple[Any, ...] | None = None,
    ) -> pd.DataFrame:
        """Query StockScreener with specified columns and filters.

        Parameters
        ----------
        columns : list[Any]
            List of StockField enums to retrieve
        where_clause : Any | None, optional
            WHERE clause or list of WHERE clauses for filtering
        sort_by : Any | None, optional
            Field enum to sort by (e.g., StockField.RELATIVE_STRENGTH_INDEX_14)
        ascending : bool, optional
            Sort direction (True for ascending, False for descending), by default True
        limit : int | None, optional
            Maximum number of results
        country : str | None, optional
            Country filter (e.g., "South Korea", "United States")
        markets : list[Any] | tuple[Any, ...] | None, optional
            Explicit tvscreener Market values passed to StockScreener.set_markets()

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
            tvscreener = _import_tvscreener()
            StockField = tvscreener.StockField
            StockScreener = tvscreener.StockScreener
            where_clauses = _normalize_where_clauses(where_clause)

            def _execute_query() -> pd.DataFrame:
                screener = StockScreener()
                if markets:
                    screener.set_markets(*markets)
                query = screener.select(*columns)

                if country:
                    query = query.where(StockField.COUNTRY == country)

                for condition in where_clauses:
                    query = query.where(condition)

                if sort_by:
                    query = query.sort_by(sort_by, ascending=ascending)

                if limit:
                    query = query.set_range(0, limit)

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

            return _normalize_result_frame(result)

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
    "TvScreenerCapabilitySnapshot",
    "TvScreenerCapabilityState",
    "TvScreenerService",
    "TvScreenerError",
    "TvScreenerRateLimitError",
    "TvScreenerMalformedRequestError",
    "TvScreenerTimeoutError",
    "get_tvscreener_service",
]
