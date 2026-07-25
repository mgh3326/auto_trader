from __future__ import annotations

import pytest

from app.services.brokers.kis.circuit_breaker import KISCircuitOpen
from app.services.invest_price_fallback import PriceFallbackResolver

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def test_kis_circuit_open_is_plain_exception():
    # Must be a plain Exception subclass so existing broad handlers catch it.
    assert issubclass(KISCircuitOpen, Exception)
    assert not issubclass(KISCircuitOpen, BaseException) or issubclass(
        KISCircuitOpen, Exception
    )


async def test_open_circuit_kis_layer_fails_through_to_toss():
    async def kis_fetch(symbols):
        raise KISCircuitOpen(45.0)  # breaker open -> ~0ms raise

    async def toss_fetch(symbols):
        return dict.fromkeys(symbols, 100.0)

    async def snapshot_fetch(symbols):
        return {}

    resolver = PriceFallbackResolver(
        kis_fetch=kis_fetch,
        toss_fetch=toss_fetch,
        snapshot_fetch=snapshot_fetch,
        market="kr",
    )
    out = await resolver.resolve(["005930", "000660"])
    # KIS layer raised KISCircuitOpen; _apply_layer caught it fail-open; Toss filled.
    assert out == {"005930": 100.0, "000660": 100.0}


async def test_open_circuit_falls_to_snapshot_when_toss_also_empty():
    async def kis_fetch(symbols):
        raise KISCircuitOpen(45.0)

    async def toss_fetch(symbols):
        return {}

    async def snapshot_fetch(symbols):
        return dict.fromkeys(symbols, 42.0)

    resolver = PriceFallbackResolver(
        kis_fetch=kis_fetch,
        toss_fetch=toss_fetch,
        snapshot_fetch=snapshot_fetch,
        market="us",
    )
    out = await resolver.resolve(["AAPL"])
    assert out == {"AAPL": 42.0}
