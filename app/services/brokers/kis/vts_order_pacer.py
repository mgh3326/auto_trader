"""ROB-892: process-local serial pacer for VTS (KIS mock) order POSTs.

SUPERSEDED — retained for backward-compat/test coverage only.

The official KIS mock REST limit is 1 request per second (per account/app-key)
applied to *every* REST call (orders AND reads, domestic AND overseas). This
process-local pacer was (a) only applied to place-order POSTs and (b) unable
to coordinate independent API/MCP/worker PIDs. ROB-892 replaces it with the
Redis-backed distributed gate in ``vts_distributed_gate.py``, enforced at the
common actual-dispatch boundary in ``base.py``. No runtime dispatch path calls
this pacer any more (single authority = the distributed gate). The class is
kept as a self-contained serial-pacer utility with its existing unit tests.

Design (historical):
- ``asyncio.Lock`` + monotonic clock → strict serial ordering.
- Injectable ``clock`` / ``sleep`` for deterministic concurrent tests.
- Module-level singleton so every mock order path shared one gate.
- No retry, no Redis, no distributed coordination (process-local scope only).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

_VTS_ORDER_MIN_INTERVAL_SECONDS = 1.0

_pacer: VTSOrderPacer | None = None


class VTSOrderPacer:
    """Process-local serial pacer enforcing a minimum interval between dispatches."""

    __slots__ = ("_min_interval", "_clock", "_sleep", "_last_dispatch", "_lock")

    def __init__(
        self,
        *,
        min_interval: float = _VTS_ORDER_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._last_dispatch: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Block until at least ``min_interval`` has passed since the last dispatch.

        Returns the actual wait time in seconds (0.0 if no wait was needed).
        """
        async with self._lock:
            now = self._clock()
            elapsed = now - self._last_dispatch
            wait = max(0.0, self._min_interval - elapsed)
            if wait > 0:
                await self._sleep(wait)
            self._last_dispatch = self._clock()
            return wait

    def reset(self) -> None:
        self._last_dispatch = 0.0


def get_vts_order_pacer() -> VTSOrderPacer:
    """Return the process-local singleton pacer."""
    global _pacer
    if _pacer is None:
        _pacer = VTSOrderPacer()
    return _pacer


def reset_vts_order_pacer() -> None:
    """Discard the singleton so the next ``get_vts_order_pacer`` creates a fresh one."""
    global _pacer
    _pacer = None
