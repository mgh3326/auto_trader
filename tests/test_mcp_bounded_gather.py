"""ROB-469 PR2: tests for the bounded_gather concurrency helper."""

from __future__ import annotations

import asyncio

import pytest

from app.mcp_server.tooling.concurrency import bounded_gather


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caps_peak_concurrency() -> None:
    active = 0
    peak = 0

    async def work(i: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return i

    factories = [lambda i=i: work(i) for i in range(10)]
    results = await bounded_gather(3, factories)
    assert results == list(range(10))  # order preserved
    assert peak <= 3  # never more than the limit concurrent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_return_exceptions_collects_errors() -> None:
    async def boom() -> int:
        raise ValueError("nope")

    async def ok() -> int:
        return 1

    results = await bounded_gather(2, [boom, ok], return_exceptions=True)
    assert isinstance(results[0], ValueError)
    assert results[1] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_returns_empty() -> None:
    assert await bounded_gather(4, []) == []
