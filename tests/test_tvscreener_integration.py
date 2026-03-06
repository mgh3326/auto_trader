from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest

from app.services.tvscreener_service import TvScreenerError, TvScreenerService


class FakeQuery:
    def __init__(self, result: pd.DataFrame) -> None:
        self._result = result
        self.where_calls: list[object] = []
        self.sort_by_calls: list[tuple[object, bool]] = []
        self.range_calls: list[tuple[int, int]] = []

    def where(self, condition: object) -> FakeQuery:
        self.where_calls.append(condition)
        return self

    def sort_by(self, field: object, *, ascending: bool) -> FakeQuery:
        self.sort_by_calls.append((field, ascending))
        return self

    def set_range(self, start: int, end: int) -> FakeQuery:
        self.range_calls.append((start, end))
        return self

    def get(self) -> pd.DataFrame:
        return self._result


class FakeCryptoScreener:
    def __init__(self, result: pd.DataFrame) -> None:
        self.query = FakeQuery(result)
        self.selected_columns: list[object] = []

    def select(self, *columns: object) -> FakeQuery:
        self.selected_columns = list(columns)
        return self.query


class FakeStockScreener(FakeCryptoScreener):
    def __init__(self, result: pd.DataFrame) -> None:
        super().__init__(result)
        self.markets: list[object] = []

    def set_markets(self, *markets: object) -> None:
        self.markets = list(markets)


class FakeField:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return cast(bool, cast(object, f"{self.label} == {other}"))


@pytest.mark.asyncio
async def test_query_crypto_screener_normalizes_real_column_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = pd.DataFrame(
        {
            "Symbol": ["UPBIT:BTCKRW"],
            "Name": ["BTCKRW"],
            "Relative Strength Index (14)": [51.3],
            "Average Directional Index (14)": [40.1],
            "Volume 24h in USD": [150_000_000.0],
            "Exchange": ["UPBIT"],
            "Change %": [1.25],
        }
    )
    screener = FakeCryptoScreener(raw)
    service = TvScreenerService()

    async def fake_fetch(
        screener_callable: Callable[[], pd.DataFrame],
        operation_name: str = "screener_query",
    ) -> pd.DataFrame:
        _ = operation_name
        screener_callable()
        return raw

    monkeypatch.setattr(service, "fetch_with_retry", fake_fetch)

    module = SimpleNamespace(CryptoScreener=lambda: screener)
    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener", lambda: module
    )

    result = await service.query_crypto_screener(
        columns=["name"], where_clause=["exchange == UPBIT"], limit=3
    )

    assert list(result.columns) == [
        "symbol",
        "name",
        "relative_strength_index_14",
        "average_directional_index_14",
        "volume_24h_in_usd",
        "exchange",
        "change_percent",
    ]
    assert screener.query.where_calls == ["exchange == UPBIT"]
    assert screener.query.range_calls == [(0, 3)]


@pytest.mark.asyncio
async def test_query_stock_screener_sets_markets_and_applies_filters_individually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = pd.DataFrame(
        {
            "Symbol": ["KRX:005930"],
            "Name": ["005930"],
            "Description": ["Samsung Electronics Co., Ltd."],
            "Active Symbol": [True],
            "Relative Strength Index (14)": [55.2],
            "Average Directional Index (14)": [20.0],
            "Volume": [1000.0],
            "Change %": [0.5],
            "Country": ["South Korea"],
        }
    )
    screener = FakeStockScreener(raw)
    service = TvScreenerService()

    async def fake_fetch(
        screener_callable: Callable[[], pd.DataFrame],
        operation_name: str = "screener_query",
    ) -> pd.DataFrame:
        _ = operation_name
        screener_callable()
        return raw

    monkeypatch.setattr(service, "fetch_with_retry", fake_fetch)
    fake_market = SimpleNamespace(KOREA="KOREA")
    module = SimpleNamespace(
        StockScreener=lambda: screener,
        StockField=SimpleNamespace(COUNTRY=FakeField("COUNTRY")),
        Market=fake_market,
    )
    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener", lambda: module
    )

    result = await service.query_stock_screener(
        columns=["symbol"],
        where_clause=["rsi <= 40", "adx >= 25"],
        limit=3,
        country="South Korea",
        markets=[fake_market.KOREA],
    )

    assert screener.markets == [fake_market.KOREA]
    assert screener.query.where_calls == [
        "COUNTRY == South Korea",
        "rsi <= 40",
        "adx >= 25",
    ]
    assert screener.query.range_calls == [(0, 3)]
    assert list(result.columns) == [
        "symbol",
        "name",
        "description",
        "active_symbol",
        "relative_strength_index_14",
        "average_directional_index_14",
        "volume",
        "change_percent",
        "country",
    ]


@pytest.mark.asyncio
async def test_query_stock_screener_raises_when_import_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService()
    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: (_ for _ in ()).throw(ImportError("missing")),
    )

    with pytest.raises(TvScreenerError, match="not installed"):
        await service.query_stock_screener(columns=[])
