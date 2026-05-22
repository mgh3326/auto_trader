"""ROB-286 — Optional Sentry/log emission helpers for the scalper.

Notifications are observation-only — they NEVER gate a decision (per
§B.C.7). All emissions are fail-open: import errors or transport
failures must not break the runner.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.services.scalping")


def log_action_taken(*, symbol: str, action_name: str, details: dict[str, Any]) -> None:
    """Single info-level log line per action taken by the runner."""
    logger.info(
        "scalper action symbol=%s action=%s details=%s",
        symbol,
        action_name,
        details,
    )


def emit_sentry_breadcrumb(*, message: str, data: dict[str, Any]) -> None:
    """Fail-open Sentry breadcrumb (best-effort)."""
    try:
        import sentry_sdk

        sentry_sdk.add_breadcrumb(category="scalping", message=message, data=data)
    except Exception:  # noqa: BLE001 — intentional fail-open
        return
