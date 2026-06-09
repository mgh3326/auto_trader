"""ROB-469 PR2: bounded-concurrency fan-out helper.

Caps how many coroutines run at once so a large fan-out (e.g. crypto-signal or
equity-price computation over many holdings) cannot explode the task count and stall
the single MCP event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def bounded_gather[T](
    limit: int,
    factories: list[Callable[[], Awaitable[T]]],
    *,
    return_exceptions: bool = False,
) -> list[T]:
    """Run ``factories`` (zero-arg coroutine factories) at most ``limit`` at a time.

    Results preserve input order, matching ``asyncio.gather``. Each element must be a
    *factory* (``() -> Awaitable``) rather than a bare coroutine so the coroutine is
    created only when a semaphore slot is free.
    """
    if not factories:
        return []
    sem = asyncio.Semaphore(limit)

    async def _run(factory: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await factory()

    return await asyncio.gather(
        *[_run(f) for f in factories], return_exceptions=return_exceptions
    )
