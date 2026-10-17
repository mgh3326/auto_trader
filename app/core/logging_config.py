"""Shared logging policy for noisy third-party libraries."""

from __future__ import annotations

import logging

HTTPX_LOG_LEVEL = logging.WARNING


def configure_dependency_log_levels() -> None:
    """Keep transport diagnostics while suppressing per-request access lines."""
    logging.getLogger("httpx").setLevel(HTTPX_LOG_LEVEL)
    # ROB-1388 R1-B2: httpcore DEBUG/TRACE lines render raw protocol
    # errors and wire details verbatim — an erroring/hostile peer can
    # reflect credential header values (e.g. X-Finnhub-Token) into them.
    # Children (httpcore.http11/http2/...) inherit the cap.
    logging.getLogger("httpcore").setLevel(HTTPX_LOG_LEVEL)
