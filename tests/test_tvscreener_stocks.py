from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _screen_kr_via_tvscreener,
    _screen_us_via_tvscreener,
)


class _Condition:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return isinstance(other, _Condition) and self.label == other.label

    def __and__(self, other: object) -> object:
        raise AssertionError("stock filters must not be combined with '&'")


class _Field:
    def __init__(self, label: str) -> None:
        self.label = label

    def __ge__(self, other: object) -> _Condition:
        return _Condition(f"{self.label}>={other}")

    def __le__(self, other: object) -> _Condition:
        return _Condition(f"{self.label}<={other}")

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return cast(bool, cast(object, _Condition(f"{self.label}=={other}")))


@pytest.fixture
def fake_tvscreener_module() -> SimpleNamespace:
    return SimpleNamespace(
        Market=SimpleNamespace(KOREA="KOREA", AMERICA="AMERICA"),
        StockField=SimpleNamespace(
            ACTIVE_SYMBOL=_Field("active_symbol"),
            DESCRIPTION=_Field("description"),
            NAME=_Field("name"),
            PRICE=_Field("price"),
            RELATIVE_STRENGTH_INDEX_14=_Field("rsi14"),
            AVERAGE_DIRECTIONAL_INDEX_14=_Field("adx14"),
            VOLUME=_Field("volume"),
            CHANGE_PERCENT=_Field("change"),
            COUNTRY=_Field("country"),
        ),
    )


@pytest.fixture
def normalized_kr_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["KRX:005930", "KRX:000660"],
            "description": ["Samsung Electronics Co., Ltd.", "SK hynix Inc."],
            "name": ["005930", "000660"],
            "price": [70000.0, 120000.0],
            "relative_strength_index_14": [32.5, 28.3],
            "average_directional_index_14": [22.4, 35.7],
            "volume": [15_000_000.0, 8_000_000.0],
            "change_percent": [2.5, -1.2],
            "country": ["South Korea", "South Korea"],
        }
    )


@pytest.fixture
def normalized_us_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["NASDAQ:AAPL", "NASDAQ:NVDA"],
            "description": ["Apple Inc.", "NVIDIA Corporation"],
            "name": ["AAPL", "NVDA"],
            "price": [175.5, 890.0],
            "relative_strength_index_14": [35.2, 47.6],
            "average_directional_index_14": [25.6, 40.1],
            "volume": [75_000_000.0, 44_000_000.0],
            "change_percent": [1.2, 0.2],
            "country": ["United States", "United States"],
        }
    )


@pytest.mark.asyncio
async def test_screen_kr_uses_market_korea_and_public_symbol_name_mapping(
    normalized_kr_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = normalized_kr_df

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_kr_via_tvscreener(limit=5)

    kwargs = service.query_stock_screener.await_args.kwargs
    assert kwargs["markets"] == [fake_tvscreener_module.Market.KOREA]
    assert kwargs["country"] is None
    assert result["error"] is None
    assert [stock["symbol"] for stock in result["stocks"]] == ["000660", "005930"]
    assert result["stocks"][1]["name"] == "Samsung Electronics Co., Ltd."


@pytest.mark.asyncio
async def test_screen_us_uses_market_america_and_country_filter(
    normalized_us_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = normalized_us_df

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(limit=5)

    kwargs = service.query_stock_screener.await_args.kwargs
    assert kwargs["markets"] == [fake_tvscreener_module.Market.AMERICA]
    assert kwargs["country"] == "United States"
    assert result["stocks"][0]["symbol"] == "AAPL"
    assert result["stocks"][0]["name"] == "Apple Inc."


@pytest.mark.asyncio
async def test_screen_us_passes_combined_filters_without_bitwise_and(
    normalized_us_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = normalized_us_df

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(max_rsi=40.0, min_adx=25.0, limit=5)

    kwargs = service.query_stock_screener.await_args.kwargs
    assert kwargs["where_clause"] == [
        fake_tvscreener_module.StockField.RELATIVE_STRENGTH_INDEX_14 <= 40.0,
        fake_tvscreener_module.StockField.AVERAGE_DIRECTIONAL_INDEX_14 >= 25.0,
    ]
    assert result["error"] is None


@pytest.mark.asyncio
async def test_screen_kr_falls_back_to_name_when_description_missing(
    normalized_kr_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    df = normalized_kr_df.copy()
    df.loc[0, "description"] = None
    service.query_stock_screener.return_value = df.iloc[[0]]

    with (
        patch(
            "app.mcp_server.tooling.analysis_screen_core._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.analysis_screen_core.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_kr_via_tvscreener(limit=1)

    assert result["stocks"][0]["name"] == "005930"


class TestStockScreeningIntegration:
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_kr_screening_real_api(self) -> None:
        pytest.importorskip("tvscreener")

        result = await _screen_kr_via_tvscreener(limit=5)

        assert result["source"] == "tvscreener"
        assert result["error"] is None
        if result["stocks"]:
            first = result["stocks"][0]
            assert ":" not in first["symbol"]
            assert first["name"]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_us_screening_real_api(self) -> None:
        pytest.importorskip("tvscreener")

        result = await _screen_us_via_tvscreener(limit=5)

        assert result["source"] == "tvscreener"
        assert result["error"] is None
        if result["stocks"]:
            first = result["stocks"][0]
            assert ":" not in first["symbol"]
            assert first["name"]
