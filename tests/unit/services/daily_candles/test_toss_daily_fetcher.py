"""Unit tests for Toss daily candle fetcher.

TDD: these tests are written first and will drive the implementation in
app/services/daily_candles/toss_daily_fetcher.py.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from app.services.brokers.toss.dto import TossCandle, TossCandlesPage
from app.services.daily_candles.toss_daily_fetcher import fetch_kr_daily_toss


class FakeTossClient:
    def __init__(self, pages: list[TossCandlesPage]) -> None:
        self._pages = iter(pages)
        self.calls: list[dict] = []

    async def candles(self, symbol, *, interval, count, before=None, adjusted=None):
        self.calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "count": count,
                "before": before,
                "adjusted": adjusted,
            }
        )
        return next(self._pages)

    async def aclose(self) -> None:
        pass


def _make_page(dates: list[str], next_before: str | None = None) -> TossCandlesPage:
    return TossCandlesPage(
        candles=[
            TossCandle(
                timestamp=f"{d}T00:00:00.000+09:00",
                open_price=Decimal("100"),
                high_price=Decimal("110"),
                low_price=Decimal("90"),
                close_price=Decimal("105"),
                volume=Decimal("1000"),
                currency="KRW",
            )
            for d in dates
        ],
        next_before=next_before,
    )


@pytest.mark.asyncio
async def test_fetch_kr_daily_toss_returns_frame_for_single_page():
    client = FakeTossClient(pages=[_make_page(["2026-06-12", "2026-06-11"])])

    frame = await fetch_kr_daily_toss(client=client, symbol="005930", n=2)

    assert isinstance(frame, pd.DataFrame)
    assert len(frame) == 2
    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume", "value"]
    assert client.calls[0]["symbol"] == "005930"
    assert client.calls[0]["interval"] == "1d"
    assert client.calls[0]["adjusted"] is True


@pytest.mark.asyncio
async def test_fetch_kr_daily_toss_paginates_until_n_bars():
    client = FakeTossClient(
        pages=[
            _make_page(["2026-06-12"], next_before="2026-06-11T00:00:00.000+09:00"),
            _make_page(["2026-06-11"], next_before=None),
        ]
    )

    frame = await fetch_kr_daily_toss(client=client, symbol="005930", n=2)

    assert len(frame) == 2
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_fetch_kr_daily_toss_returns_empty_frame_when_no_data():
    client = FakeTossClient(pages=[TossCandlesPage(candles=[], next_before=None)])

    frame = await fetch_kr_daily_toss(client=client, symbol="005930", n=5)

    assert frame.empty
