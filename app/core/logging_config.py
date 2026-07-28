"""Shared logging policy for noisy third-party libraries."""

from __future__ import annotations

import logging

HTTPX_LOG_LEVEL = logging.WARNING


def configure_dependency_log_levels() -> None:
    """Keep transport diagnostics while suppressing per-request access lines."""
    logging.getLogger("httpx").setLevel(HTTPX_LOG_LEVEL)
