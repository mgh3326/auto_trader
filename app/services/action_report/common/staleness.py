"""ROB-274 — shared staleness constants for pending-order rationale generation.

KR/US use market-session expiry handled by the broker; crypto orders can
persist 24/7, so we apply an explicit age threshold instead. Threading
this through a module makes the value greppable and overridable from
settings if needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

PENDING_ORDER_STALENESS_HOURS_CRYPTO: Final[int] = 24


def is_crypto_pending_order_stale(
    placed_at: datetime, *, now: datetime | None = None
) -> bool:
    """Return True if a crypto pending order is older than the staleness threshold."""

    reference = now or datetime.now(tz=UTC)
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=UTC)
    return reference - placed_at > timedelta(
        hours=PENDING_ORDER_STALENESS_HOURS_CRYPTO
    )
