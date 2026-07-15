"""ROB-892: VTS order pacer — process-local serial gate for mock order POSTs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.services.brokers.kis.vts_order_pacer import VTSOrderPacer


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _make_instant_sleep(clock: FakeClock) -> Callable[[float], Awaitable[None]]:
    """Return a sleep that advances the fake clock instead of blocking."""

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)

    return _sleep


@pytest.mark.asyncio
async def test_first_dispatch_no_wait():
    clock = FakeClock()
    pacer = VTSOrderPacer(clock=clock, sleep=_make_instant_sleep(clock))
    wait = await pacer.acquire()
    assert wait == 0.0


@pytest.mark.asyncio
async def test_second_dispatch_within_interval_waits():
    clock = FakeClock()
    pacer = VTSOrderPacer(clock=clock, sleep=_make_instant_sleep(clock))
    await pacer.acquire()
    clock.advance(0.3)
    wait = await pacer.acquire()
    assert wait == pytest.approx(0.7, abs=0.01)


@pytest.mark.asyncio
async def test_dispatch_after_interval_no_wait():
    clock = FakeClock()
    pacer = VTSOrderPacer(clock=clock, sleep=_make_instant_sleep(clock))
    await pacer.acquire()
    clock.advance(1.5)
    wait = await pacer.acquire()
    assert wait == 0.0


@pytest.mark.asyncio
async def test_concurrent_buy_sell_share_gate():
    clock = FakeClock()
    pacer = VTSOrderPacer(clock=clock, sleep=_make_instant_sleep(clock))

    dispatch_times: list[float] = []

    async def _dispatch(side: str) -> str:
        await pacer.acquire()
        dispatch_times.append(clock())
        return side

    results = await asyncio.gather(_dispatch("buy"), _dispatch("sell"))

    assert set(results) == {"buy", "sell"}
    assert len(dispatch_times) == 2
    gap = dispatch_times[1] - dispatch_times[0]
    assert gap >= 1.0


@pytest.mark.asyncio
async def test_three_parallel_calls_serialize_without_burst():
    clock = FakeClock()
    pacer = VTSOrderPacer(clock=clock, sleep=_make_instant_sleep(clock))

    dispatch_times: list[float] = []

    async def _dispatch(idx: int) -> int:
        await pacer.acquire()
        dispatch_times.append(clock())
        return idx

    await asyncio.gather(*[_dispatch(i) for i in range(3)])

    assert len(dispatch_times) == 3
    for i in range(1, len(dispatch_times)):
        gap = dispatch_times[i] - dispatch_times[i - 1]
        assert gap >= 1.0


@pytest.mark.asyncio
async def test_reset_clears_last_dispatch():
    clock = FakeClock()
    pacer = VTSOrderPacer(clock=clock, sleep=_make_instant_sleep(clock))
    await pacer.acquire()
    clock.advance(0.1)
    pacer.reset()
    wait = await pacer.acquire()
    assert wait == 0.0


@pytest.mark.asyncio
async def test_real_sleep_does_not_block_other_tasks():
    """With real asyncio.sleep, a waiting pacer yields control to the event loop."""

    pacer = VTSOrderPacer(min_interval=0.05)
    flag = False

    async def _background():
        nonlocal flag
        await asyncio.sleep(0.01)
        flag = True

    await pacer.acquire()
    bg = asyncio.create_task(_background())
    await pacer.acquire()
    await bg
    assert flag
