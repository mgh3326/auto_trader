"""Retry executor and exceptions for tvscreener requests."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

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

async def fetch_tvscreener_with_retry(
    screener_callable: Callable[[], pd.DataFrame],
    *,
    operation_name: str,
    max_retries: int,
    base_delay: float,
    timeout: float,
) -> pd.DataFrame:
    """Execute a synchronous tvscreener callable with retry and timeout handling."""
    for attempt in range(max_retries):
        start_time = time.time()
        try:
            logger.debug(
                "Executing %s (attempt %d/%d)",
                operation_name,
                attempt + 1,
                max_retries,
            )
            result = await asyncio.wait_for(
                asyncio.to_thread(screener_callable),
                timeout=timeout,
            )
            elapsed = time.time() - start_time
            logger.info(
                "%s completed successfully in %.2fs (attempt %d/%d)",
                operation_name,
                elapsed,
                attempt + 1,
                max_retries,
            )
            return result

        except TimeoutError as exc:
            elapsed = time.time() - start_time
            logger.error(
                "%s timed out after %.2fs (attempt %d/%d)",
                operation_name,
                elapsed,
                attempt + 1,
                max_retries,
            )
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                logger.info("Retrying after %.2fs delay...", delay)
                await asyncio.sleep(delay)
                continue
            raise TvScreenerTimeoutError(
                f"{operation_name} timed out after {timeout}s"
            ) from exc

        except Exception as exc:
            exc_type = type(exc).__name__
            exc_msg = str(exc)

            if "malformed" in exc_msg.lower() or exc_type == "MalformedRequestException":
                logger.warning(
                    "%s received malformed request error (attempt %d/%d): %s: %s",
                    operation_name,
                    attempt + 1,
                    max_retries,
                    exc_type,
                    exc_msg,
                )
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.info(
                        "Rate limit suspected, retrying after %.2fs delay...", delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise TvScreenerMalformedRequestError(
                    f"{operation_name} failed with malformed request: {exc_msg}"
                ) from exc

            if "rate limit" in exc_msg.lower() or "too many requests" in exc_msg.lower():
                logger.warning(
                    "%s hit rate limit (attempt %d/%d): %s",
                    operation_name,
                    attempt + 1,
                    max_retries,
                    exc_msg,
                )
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.info("Retrying after %.2fs delay...", delay)
                    await asyncio.sleep(delay)
                    continue
                raise TvScreenerRateLimitError(
                    f"{operation_name} exceeded rate limit: {exc_msg}"
                ) from exc

            logger.error(
                "%s failed with unexpected error (attempt %d/%d): %s: %s",
                operation_name,
                attempt + 1,
                max_retries,
                exc_type,
                exc_msg,
                exc_info=True,
            )
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                logger.info("Retrying after %.2fs delay...", delay)
                await asyncio.sleep(delay)
                continue
            raise TvScreenerError(
                f"{operation_name} failed: {exc_type}: {exc_msg}"
            ) from exc

    raise TvScreenerError(f"{operation_name} failed after {max_retries} attempts")
