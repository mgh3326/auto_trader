"""ROB-285 — Binance adapter errors."""

from __future__ import annotations


class BinanceAdapterError(Exception):
    """Base class for Binance adapter errors."""


class BinanceLiveHostBlocked(BinanceAdapterError):
    """Raised when the transport detects a request to a non-allowlisted host."""


class BinanceSignedEndpointAttempted(BinanceAdapterError):
    """Raised when the transport detects an API-key header on a public request."""


class BinanceRateLimited(BinanceAdapterError):
    """Raised when REST 429/418 is received; carries Retry-After seconds."""

    def __init__(self, retry_after_seconds: float, message: str = "") -> None:
        super().__init__(
            message or f"Rate-limited; retry after {retry_after_seconds}s"
        )
        self.retry_after_seconds = retry_after_seconds


class BinanceBackfillCapExceeded(BinanceAdapterError):
    """Raised when a gap exceeds REST backfill caps.

    Caller should mark the instrument as ``manual_backfill_required`` and
    stop trading it until an operator manually clears the flag.
    """
