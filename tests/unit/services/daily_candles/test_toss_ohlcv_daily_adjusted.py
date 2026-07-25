"""ROB-706 data-equivalence guard: the KR-daily Toss fallback (fetch_daily_toss_frame)
MUST request interval='1d' and adjusted=True so it matches the KIS adj=True series
already stored as source='kis' in kr_candles_1d. An unadjusted Toss frame spliced
into the daily series would corrupt it at every historical split/dividend."""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.market_data import toss_ohlcv

pytestmark = pytest.mark.unit


class _FakeClient:
    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_fetch_daily_toss_frame_requests_adjusted_1d(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_fetch(
        *, client, symbol, interval, count, before=None, adjusted=None, max_pages=20
    ):
        captured["interval"] = interval
        captured["adjusted"] = adjusted
        captured["symbol"] = symbol
        return pd.DataFrame(
            columns=[
                "datetime",
                "date",
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "value",
            ]
        )

    monkeypatch.setattr(toss_ohlcv, "fetch_toss_candles_frame", fake_fetch)
    monkeypatch.setattr(
        toss_ohlcv.TossReadClient,
        "from_settings",
        classmethod(lambda cls: _FakeClient()),
    )

    await toss_ohlcv.fetch_daily_toss_frame(symbol="005930", count=200)

    assert captured["symbol"] == "005930"
    assert captured["interval"] == "1d"
    assert captured["adjusted"] is True
