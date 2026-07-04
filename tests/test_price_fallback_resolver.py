from __future__ import annotations

import pytest

from app.services.invest_price_fallback import PriceFallbackResolver

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _fetcher(mapping, *, calls=None, boom=False):
    async def _f(symbols):
        if calls is not None:
            calls.append(list(symbols))
        if boom:
            raise RuntimeError("layer down")
        return {s: mapping.get(s) for s in symbols}

    return _f


async def test_kis_success_skips_toss_and_snapshot():
    toss_calls, snap_calls = [], []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"005930": 70000.0, "034020": 18000.0}),
        toss_fetch=_fetcher({}, calls=toss_calls),
        snapshot_fetch=_fetcher({}, calls=snap_calls),
        market="kr",
    )
    out = await resolver.resolve(["005930", "034020"])
    assert out == pytest.approx({"005930": 70000.0, "034020": 18000.0})
    assert toss_calls == []  # never consulted
    assert snap_calls == []


async def test_toss_fills_only_the_kis_misses():
    toss_calls = []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0, "B": None, "C": None}),
        toss_fetch=_fetcher({"B": 20.0}, calls=toss_calls),  # C stays missing
        snapshot_fetch=_fetcher({"C": 30.0}),
        market="us",
    )
    out = await resolver.resolve(["A", "B", "C"])
    assert out == pytest.approx({"A": 10.0, "B": 20.0, "C": 30.0})
    assert toss_calls == [["B", "C"]]  # only KIS misses, batched once


async def test_toss_disabled_falls_through_to_snapshot():
    snap_calls = []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": None}),
        toss_fetch=None,  # disabled
        snapshot_fetch=_fetcher({"A": 99.0}, calls=snap_calls),
        market="kr",
    )
    out = await resolver.resolve(["A"])
    assert out == pytest.approx({"A": 99.0})
    assert snap_calls == [["A"]]


async def test_all_layers_fail_open_to_none_without_raising():
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({}, boom=True),  # KIS outage
        toss_fetch=_fetcher({}, boom=True),  # Toss also down
        snapshot_fetch=_fetcher({}, boom=True),  # snapshot query errors
        market="us",
    )
    out = await resolver.resolve(["A", "B"])
    assert out == {"A": None, "B": None}  # never raises


async def test_empty_input_returns_empty():
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({}),
        toss_fetch=None,
        snapshot_fetch=_fetcher({}),
        market="kr",
    )
    assert await resolver.resolve([]) == {}


async def test_snapshot_only_runs_for_still_missing():
    snap_calls = []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0, "B": None}),
        toss_fetch=_fetcher({"B": None}),  # Toss has nothing for B
        snapshot_fetch=_fetcher({"B": 5.0}, calls=snap_calls),
        market="kr",
    )
    out = await resolver.resolve(["A", "B"])
    assert out == pytest.approx({"A": 10.0, "B": 5.0})
    assert snap_calls == [["B"]]  # A already resolved by KIS
