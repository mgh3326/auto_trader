from __future__ import annotations

import pandas as pd
import pytest

import app.services.price_provider as price_provider
from app.services.price_provider import YahooUsPriceProvider


async def _frame_with_close(value: float) -> pd.DataFrame:
    return pd.DataFrame([{"close": value}])


async def _empty_frame(_symbol: str) -> pd.DataFrame:
    return pd.DataFrame()


async def _fetch_raises(_symbol: str) -> pd.DataFrame:
    raise RuntimeError("upstream timeout")


@pytest.mark.asyncio
async def test_yahoo_provider_fetch_many_all_success(monkeypatch) -> None:
    calls: list[str] = []

    async def fetch_price(symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        return await _frame_with_close({"AAPL": 182.3, "MSFT": 415.8}[symbol])

    monkeypatch.setattr(price_provider.yahoo_service, "fetch_price", fetch_price)
    provider = YahooUsPriceProvider(max_concurrency=3)

    prices, errors = await provider.fetch_many(["aapl", "MSFT"])

    assert prices == {"AAPL": 182.3, "MSFT": 415.8}
    assert errors == []
    assert calls == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_yahoo_provider_fetch_many_partial_failure(monkeypatch) -> None:
    async def fetch_price(symbol: str) -> pd.DataFrame:
        if symbol == "MSFT":
            raise RuntimeError("yfinance down")
        return await _frame_with_close(201.5)

    monkeypatch.setattr(price_provider.yahoo_service, "fetch_price", fetch_price)
    provider = YahooUsPriceProvider(max_concurrency=2)

    prices, errors = await provider.fetch_many(["AAPL", "MSFT"])

    assert prices == {"AAPL": 201.5}
    assert len(errors) == 1
    assert errors[0].symbol == "MSFT"
    assert errors[0].source == "yahoo"
    assert "yfinance down" in errors[0].error


@pytest.mark.asyncio
async def test_yahoo_provider_fetch_many_all_failure(monkeypatch) -> None:
    monkeypatch.setattr(price_provider.yahoo_service, "fetch_price", _fetch_raises)
    provider = YahooUsPriceProvider(max_concurrency=4)

    prices, errors = await provider.fetch_many(["AAPL", "TSLA"])

    assert prices == {}
    assert [item.symbol for item in errors] == ["AAPL", "TSLA"]
    assert all(item.source == "yahoo" for item in errors)


@pytest.mark.asyncio
async def test_yahoo_provider_fetch_many_deduplicates_symbols(monkeypatch) -> None:
    calls: list[str] = []

    async def fetch_price(symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        return await _frame_with_close(99.0)

    monkeypatch.setattr(price_provider.yahoo_service, "fetch_price", fetch_price)
    provider = YahooUsPriceProvider(max_concurrency=1)

    prices, errors = await provider.fetch_many(["AAPL", "aapl", " AAPL "])

    assert prices == {"AAPL": 99.0}
    assert errors == []
    assert calls == ["AAPL"]


@pytest.mark.asyncio
async def test_yahoo_provider_fetch_many_handles_empty_frame(monkeypatch) -> None:
    monkeypatch.setattr(price_provider.yahoo_service, "fetch_price", _empty_frame)
    provider = YahooUsPriceProvider(max_concurrency=1)

    prices, errors = await provider.fetch_many(["AAPL"])

    assert prices == {}
    assert len(errors) == 1
    assert errors[0].symbol == "AAPL"
    assert "empty" in errors[0].error.lower()
