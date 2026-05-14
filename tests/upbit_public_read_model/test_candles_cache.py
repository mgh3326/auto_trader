import pandas as pd
import pytest

from app.services.upbit_public_read_model.candles_cache import CandlesCache


@pytest.mark.asyncio
async def test_candles_cache_rejects_intraday_period():
    cache = CandlesCache(closed_candles_getter=lambda *a, **k: None)
    with pytest.raises(ValueError):
        await cache.get("KRW-BTC", period="1m", count=5)


@pytest.mark.asyncio
async def test_candles_cache_converts_dataframe_rows():
    async def getter(market, count, period):
        return pd.DataFrame(
            [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3, "value": 4}],
            index=[pd.Timestamp("2026-05-14")],
        )

    block = await CandlesCache(closed_candles_getter=getter).get(
        "KRW-BTC", period="day", count=1
    )
    assert block.meta.state == "fresh"
    assert block.rows[0]["open"] == 1


@pytest.mark.asyncio
async def test_candles_cache_unavailable_on_exception():
    async def getter(*args, **kwargs):
        raise RuntimeError("down")

    block = await CandlesCache(closed_candles_getter=getter).get(
        "KRW-BTC", period="day", count=1
    )
    assert block.meta.state == "unavailable"
