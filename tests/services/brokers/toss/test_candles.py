from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from app.services.brokers.toss.candles import (
    fetch_toss_candles_frame,
    toss_candles_page_to_frame,
)
from app.services.brokers.toss.dto import TossCandle, TossCandlesPage


def test_toss_candles_page_to_frame_sorts_ascending_and_computes_value() -> None:
    page = TossCandlesPage(
        candles=[
            TossCandle(
                timestamp="2026-06-12T09:14:00.000+09:00",
                open_price=Decimal("330250"),
                high_price=Decimal("330500"),
                low_price=Decimal("330000"),
                close_price=Decimal("330500"),
                volume=Decimal("10"),
                currency="KRW",
            ),
            TossCandle(
                timestamp="2026-06-12T09:13:00.000+09:00",
                open_price=Decimal("329000"),
                high_price=Decimal("331500"),
                low_price=Decimal("328500"),
                close_price=Decimal("330000"),
                volume=Decimal("20"),
                currency="KRW",
            ),
        ],
        next_before="2026-06-12T09:12:00.000+09:00",
    )

    frame = toss_candles_page_to_frame(page)

    assert list(frame["datetime"]) == [
        pd.Timestamp("2026-06-12T09:13:00.000+09:00"),
        pd.Timestamp("2026-06-12T09:14:00.000+09:00"),
    ]
    assert list(frame["open"]) == [329000.0, 330250.0]
    assert list(frame["close"]) == [330000.0, 330500.0]
    assert list(frame["volume"]) == [20.0, 10.0]
    assert list(frame["value"]) == [6600000.0, 3305000.0]


@pytest.mark.asyncio
async def test_fetch_toss_candles_frame_paginates_until_count() -> None:
    calls: list[str | None] = []

    class FakeClient:
        async def candles(self, symbol, *, interval, count, before=None, adjusted=None):
            calls.append(before)
            if before is None:
                return TossCandlesPage(
                    candles=[
                        TossCandle(
                            timestamp="2026-06-12T00:00:00.000+09:00",
                            open_price=Decimal("10"),
                            high_price=Decimal("11"),
                            low_price=Decimal("9"),
                            close_price=Decimal("10"),
                            volume=Decimal("100"),
                            currency="KRW",
                        )
                    ],
                    next_before="cursor-1",
                )
            return TossCandlesPage(
                candles=[
                    TossCandle(
                        timestamp="2026-06-11T00:00:00.000+09:00",
                        open_price=Decimal("8"),
                        high_price=Decimal("9"),
                        low_price=Decimal("7"),
                        close_price=Decimal("8"),
                        volume=Decimal("200"),
                        currency="KRW",
                    )
                ],
                next_before=None,
            )

    frame = await fetch_toss_candles_frame(
        client=FakeClient(),
        symbol="005930",
        interval="1d",
        count=2,
        adjusted=True,
    )

    assert calls == [None, "cursor-1"]
    assert list(frame["close"]) == [8.0, 10.0]
