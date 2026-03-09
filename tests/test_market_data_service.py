from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.domain_errors import UpstreamUnavailableError, ValidationError
from app.services.market_data import service as market_data_service
from app.services.market_data.contracts import Candle


@pytest.mark.asyncio
async def test_get_kr_volume_rank_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = [{"mksc_shrn_iscd": "005930", "acml_vol": "12345", "prdy_ctrt": "-3.2"}]

    class DummyKIS:
        async def volume_rank(self):
            return expected

    monkeypatch.setattr(market_data_service, "KISClient", lambda: DummyKIS())

    actual = await market_data_service.get_kr_volume_rank()

    assert actual == expected


@pytest.mark.asyncio
async def test_get_kr_volume_rank_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingKIS:
        async def volume_rank(self):
            raise RuntimeError("upstream failed")

    monkeypatch.setattr(market_data_service, "KISClient", lambda: FailingKIS())

    with pytest.raises(UpstreamUnavailableError, match="upstream failed"):
        await market_data_service.get_kr_volume_rank()


@pytest.mark.asyncio
async def test_get_ohlcv_rejects_invalid_period_message() -> None:
    with pytest.raises(
        ValidationError,
        match="period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'",
    ):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="hour",
            count=10,
        )


@pytest.mark.asyncio
async def test_get_ohlcv_non_kr_minute_period_rejected_for_us() -> None:
    with pytest.raises(
        ValidationError,
        match="period '5m' is not supported for us equity",
    ):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="5m",
            count=10,
        )


@pytest.mark.asyncio
async def test_get_ohlcv_kr_intraday_uses_shared_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:05:00"),
                "date": dt.date(2026, 2, 23),
                "time": dt.time(9, 5, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 1200.0,
                "value": 120000.0,
                "session": "REGULAR",
                "venues": ["KRX", "NTX"],
            }
        ]
    )
    read_mock = AsyncMock(return_value=frame)
    monkeypatch.setattr(market_data_service, "read_kr_intraday_candles", read_mock)

    candles = await market_data_service.get_ohlcv(
        symbol="005930",
        market="kr",
        period="5m",
        count=3,
    )

    read_mock.assert_awaited_once_with(
        symbol="005930",
        period="5m",
        count=3,
        end_date=None,
    )
    assert len(candles) == 1
    candle = candles[0]
    assert isinstance(candle, Candle)
    assert candle.symbol == "005930"
    assert candle.market == "equity_kr"
    assert candle.period == "5m"
    assert candle.timestamp == dt.datetime(2026, 2, 23, 9, 5, 0)
