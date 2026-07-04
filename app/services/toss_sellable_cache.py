"""ROB-701 — process-global short-TTL cache for the Toss per-symbol
sellable-quantity fanout on /invest home & account-panel.

The Toss ``GET /api/v1/sellable-quantity`` endpoint is in the ORDER_INFO
rate-limit group (6 TPS / 3 TPS peak), so fanning it out per holding serializes
to ~N/6 s. This cache collapses repeated /invest loads to 0 calls within the TTL;
only the invest_home reader opts in (the MCP / sell-classification path stays
uncached and fresh). enabled=False => always miss => today's fanout-every-load.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal

from app.core.config import settings


class TossSellableCache:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        now: Callable[[], float] = time.monotonic,
        enabled: bool = True,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._now = now
        self._enabled = enabled
        # symbol -> (expires_at_monotonic, value)
        self._entries: dict[str, tuple[float, Decimal]] = {}

    def get(self, symbol: str) -> Decimal | None:
        if not self._enabled:
            return None
        entry = self._entries.get(symbol)
        if entry is None:
            return None
        expires_at, value = entry
        if self._now() >= expires_at:
            # Expired — evict so the map does not grow unbounded on churn.
            self._entries.pop(symbol, None)
            return None
        return value

    def put(self, symbol: str, value: Decimal) -> None:
        if not self._enabled:
            return
        self._entries[symbol] = (self._now() + self._ttl, value)

    def clear(self) -> None:
        self._entries.clear()


_shared_sellable_cache: TossSellableCache | None = None


def get_shared_sellable_cache() -> TossSellableCache:
    """Process-global cache shared by every /invest reader in the process, so a
    warm entry from one surface (home) serves the next (account-panel)."""
    global _shared_sellable_cache
    if _shared_sellable_cache is None:
        _shared_sellable_cache = TossSellableCache(
            ttl_seconds=float(
                getattr(settings, "toss_sellable_cache_ttl_seconds", 45.0)
            ),
            enabled=bool(getattr(settings, "toss_sellable_cache_enabled", True)),
        )
    return _shared_sellable_cache


def reset_shared_sellable_cache() -> None:
    """Test hook: drop the process-global cache so suites start clean."""
    global _shared_sellable_cache
    _shared_sellable_cache = None
