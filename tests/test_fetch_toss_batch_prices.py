from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import TossPrice
from app.services.invest_price_fallback import fetch_toss_batch_prices

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeToss:
    def __init__(self, prices, *, boom=False):
        self._prices = prices  # {symbol: last_price}
        self._boom = boom
        self.calls: list[list[str]] = []

    async def prices(self, symbols):
        self.calls.append(list(symbols))
        if self._boom:
            raise RuntimeError("toss down")
        return [
            TossPrice(
                symbol=s,
                timestamp=None,
                last_price=Decimal(str(self._prices[s])),
                currency="KRW",
            )
            for s in symbols
            if s in self._prices
        ]


async def test_returns_float_prices_keyed_by_symbol():
    client = _FakeToss({"005930": "70500", "034020": "18000"})
    out = await fetch_toss_batch_prices(client, ["005930", "034020"])
    assert out == pytest.approx({"005930": 70500.0, "034020": 18000.0})
    assert client.calls == [["005930", "034020"]]  # ONE batch, not per-symbol


async def test_symbols_missing_from_toss_are_absent():
    client = _FakeToss({"005930": "70500"})
    out = await fetch_toss_batch_prices(client, ["005930", "999999"])
    assert set(out) == {"005930"}


async def test_chunks_over_200_into_multiple_batches():
    symbols = [f"S{i:04d}" for i in range(201)]
    client = _FakeToss(dict.fromkeys(symbols, "1"))
    out = await fetch_toss_batch_prices(client, symbols)
    assert len(out) == 201
    assert [len(c) for c in client.calls] == [200, 1]  # 1..200 batch limit


async def test_empty_symbols_makes_no_call():
    client = _FakeToss({})
    assert await fetch_toss_batch_prices(client, []) == {}
    assert client.calls == []


async def test_error_is_fail_open_returns_empty():
    client = _FakeToss({"005930": "70500"}, boom=True)
    assert await fetch_toss_batch_prices(client, ["005930"]) == {}  # no raise


async def test_case_insensitive_match_returns_requested_symbol_key():
    # Toss echoes upper; requested key is preserved for the resolver
    client = _FakeToss({"AAPL": "222.5"})
    out = await fetch_toss_batch_prices(client, ["aapl"])
    assert out == pytest.approx({"aapl": 222.5})
