from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _screen_kr_via_tvscreener,
    _screen_us_via_tvscreener,
    normalize_screen_request,
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
            MARKET_CAPITALIZATION=_Field("market_cap"),
            PRICE_TO_EARNINGS_RATIO_TTM=_Field("pe_ttm"),
            PRICE_TO_BOOK_FQ=_Field("pbr_fq"),
            DIVIDEND_YIELD_FORWARD=_Field("dividend_yield_forward"),
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
            "market_capitalization": [2_800_000_000_000.0, 2_200_000_000_000.0],
            "price_to_earnings_ratio_ttm": [28.5, 44.1],
            "dividend_yield_forward": [0.005, 0.0003],
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

    async def mock_fetch_stock_all_cached(market: str):
        if market == "STK":
            return [
                {
                    "code": "005930",
                    "short_code": "005930",
                    "name": "삼성전자",
                    "market": "KOSPI",
                    "market_cap": 4800000,
                },
                {
                    "code": "000660",
                    "short_code": "000660",
                    "name": "SK하이닉스",
                    "market": "KOSPI",
                    "market_cap": 1500000,
                },
            ]
        return []

    with (
        patch(
            "app.mcp_server.tooling.screening.kr._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.fetch_stock_all_cached",
            side_effect=mock_fetch_stock_all_cached,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.fetch_valuation_all_cached",
            AsyncMock(return_value={}),
        ),
    ):
        result = await _screen_kr_via_tvscreener(limit=5)

    kwargs = service.query_stock_screener.await_args.kwargs
    assert kwargs["markets"] == [fake_tvscreener_module.Market.KOREA]
    assert kwargs["country"] is None
    assert result["error"] is None
    assert [stock["symbol"] for stock in result["stocks"]] == ["005930", "000660"]
    assert result["stocks"][0]["name"] == "Samsung Electronics Co., Ltd."


@pytest.mark.asyncio
async def test_screen_us_uses_market_america_and_country_filter(
    normalized_us_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = normalized_us_df

    with (
        patch(
            "app.mcp_server.tooling.screening.us._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.us.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(limit=5, sort_order="asc")

    kwargs = service.query_stock_screener.await_args.kwargs
    assert kwargs["markets"] == [fake_tvscreener_module.Market.AMERICA]
    assert kwargs["country"] == "United States"
    assert [stock["symbol"] for stock in result["stocks"]] == ["AAPL", "NVDA"]
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
            "app.mcp_server.tooling.screening.us._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.us.TvScreenerService",
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
async def test_screen_us_queries_and_maps_optional_analyst_fields(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    fake_tvscreener_module.StockField.SECTOR = _Field("sector")
    fake_tvscreener_module.StockField.RECOMMENDATION_BUY = _Field("recommendation_buy")
    fake_tvscreener_module.StockField.RECOMMENDATION_HOLD = _Field(
        "recommendation_hold"
    )
    fake_tvscreener_module.StockField.RECOMMENDATION_SELL = _Field(
        "recommendation_sell"
    )
    fake_tvscreener_module.StockField.PRICE_TARGET_AVERAGE = _Field(
        "price_target_average"
    )

    service = AsyncMock()
    service.query_stock_screener.return_value = pd.DataFrame(
        {
            "symbol": ["NASDAQ:AAPL"],
            "description": ["Apple Inc."],
            "name": ["AAPL"],
            "price": [175.5],
            "relative_strength_index_14": [35.2],
            "average_directional_index_14": [25.6],
            "volume": [75_000_000.0],
            "change_percent": [1.2],
            "market_capitalization": [2_800_000_000_000.0],
            "price_to_earnings_ratio_ttm": [28.5],
            "dividend_yield_forward": [0.005],
            "sector": ["Technology"],
            "recommendation_buy": [22],
            "recommendation_hold": [14],
            "recommendation_sell": [2],
            "price_target_average": [205.0],
            "country": ["United States"],
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.screening.us._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.us.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(category="Technology", limit=5)

    kwargs = service.query_stock_screener.await_args.kwargs
    assert fake_tvscreener_module.StockField.SECTOR in kwargs["columns"]
    assert fake_tvscreener_module.StockField.RECOMMENDATION_BUY in kwargs["columns"]
    assert fake_tvscreener_module.StockField.RECOMMENDATION_HOLD in kwargs["columns"]
    assert fake_tvscreener_module.StockField.RECOMMENDATION_SELL in kwargs["columns"]
    assert fake_tvscreener_module.StockField.PRICE_TARGET_AVERAGE in kwargs["columns"]
    assert kwargs["where_clause"] == [
        fake_tvscreener_module.StockField.SECTOR == "Technology"
    ]
    assert result["error"] is None
    assert result["stocks"][0]["sector"] == "Technology"
    assert result["stocks"][0]["analyst_buy"] == 22
    assert result["stocks"][0]["analyst_hold"] == 14
    assert result["stocks"][0]["analyst_sell"] == 2
    assert result["stocks"][0]["avg_target"] == 205.0
    assert result["stocks"][0]["upside_pct"] == 16.81


@pytest.mark.asyncio
async def test_screen_kr_joins_requested_submarket_and_valuation_data(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = pd.DataFrame(
        {
            "symbol": ["KRX:005930", "KRX:035720"],
            "description": ["Samsung Electronics Co., Ltd.", "Kakao Corp."],
            "name": ["005930", "035720"],
            "price": [70000.0, 48000.0],
            "relative_strength_index_14": [29.5, 18.2],
            "average_directional_index_14": [20.0, 17.0],
            "volume": [15_000_000.0, 8_000_000.0],
            "change_percent": [1.1, 2.3],
        }
    )

    async def mock_fetch_stock_all_cached(market: str):
        if market == "STK":
            return [
                {
                    "code": "005930",
                    "short_code": "005930",
                    "name": "삼성전자",
                    "market": "KOSPI",
                    "market_cap": 4800000,
                    "close": 70000.0,
                }
            ]
        if market == "KSQ":
            return [
                {
                    "code": "035720",
                    "short_code": "035720",
                    "name": "카카오",
                    "market": "KOSDAQ",
                    "market_cap": 220000,
                    "close": 48000.0,
                }
            ]
        return []

    async def mock_fetch_valuation_all_cached(market: str):
        assert market == "STK"
        return {
            "005930": {"per": 12.5, "pbr": 1.2, "dividend_yield": 0.0256},
            "035720": {"per": 18.4, "pbr": 1.8, "dividend_yield": 0.0},
        }

    with (
        patch(
            "app.mcp_server.tooling.screening.kr._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.fetch_stock_all_cached",
            side_effect=mock_fetch_stock_all_cached,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.fetch_valuation_all_cached",
            side_effect=mock_fetch_valuation_all_cached,
        ),
    ):
        result = await _screen_kr_via_tvscreener(
            market="kospi",
            max_rsi=35.0,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

    assert result["count"] == 1
    assert [stock["symbol"] for stock in result["stocks"]] == ["005930"]
    stock = result["stocks"][0]
    assert stock["market"] == "KOSPI"
    assert stock["market_cap"] == 4800000
    assert stock["per"] == 12.5
    assert stock["pbr"] == 1.2
    assert stock["dividend_yield"] == 0.0256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_order", "expected_symbols"),
    [("asc", ["NVDA", "AAPL"]), ("desc", ["AAPL", "NVDA"])],
)
async def test_screen_us_honors_sort_order(
    normalized_us_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
    sort_order: str,
    expected_symbols: list[str],
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = normalized_us_df

    with (
        patch(
            "app.mcp_server.tooling.screening.us._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.us.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(
            sort_by="change_rate",
            sort_order=sort_order,
            limit=5,
        )

    assert [stock["symbol"] for stock in result["stocks"]] == expected_symbols
    assert result["filters_applied"]["sort_order"] == sort_order


@pytest.mark.asyncio
async def test_screen_us_adds_valuation_filters_and_applies_missing_value_backstop(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = pd.DataFrame(
        {
            "symbol": ["NASDAQ:AAPL", "NASDAQ:NVDA", "NASDAQ:TSLA"],
            "description": ["Apple Inc.", "NVIDIA Corporation", "Tesla, Inc."],
            "name": ["AAPL", "NVDA", "TSLA"],
            "price": [175.5, 890.0, 240.0],
            "relative_strength_index_14": [35.2, 47.6, 28.0],
            "average_directional_index_14": [25.6, 40.1, 20.0],
            "volume": [75_000_000.0, 44_000_000.0, 60_000_000.0],
            "change_percent": [1.2, 0.2, -0.5],
            "market_capitalization": [2_800_000_000_000.0, 2_200_000_000_000.0, None],
            "price_to_earnings_ratio_ttm": [28.5, 44.1, None],
            "dividend_yield_forward": [0.005, 0.0003, None],
            "country": ["United States", "United States", "United States"],
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.screening.us._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.us.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(
            min_market_cap=2_000_000_000_000.0,
            max_per=30.0,
            min_dividend_yield=0.004,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

    kwargs = service.query_stock_screener.await_args.kwargs
    assert kwargs["where_clause"] == [
        fake_tvscreener_module.StockField.MARKET_CAPITALIZATION >= 2_000_000_000_000.0,
        fake_tvscreener_module.StockField.PRICE_TO_EARNINGS_RATIO_TTM <= 30.0,
        fake_tvscreener_module.StockField.DIVIDEND_YIELD_FORWARD >= 0.004,
    ]
    assert result["count"] == 1
    assert [stock["symbol"] for stock in result["stocks"]] == ["AAPL"]
    assert result["stocks"][0]["market_cap"] == 2_800_000_000_000.0
    assert result["stocks"][0]["per"] == 28.5
    assert result["stocks"][0]["dividend_yield"] == 0.005


@pytest.mark.asyncio
async def test_screen_us_drops_rows_without_usable_price(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = pd.DataFrame(
        {
            "symbol": ["NASDAQ:AAPL", "NASDAQ:NVDA", "NASDAQ:MSFT"],
            "description": [
                "Apple Inc.",
                "NVIDIA Corporation",
                "Microsoft Corporation",
            ],
            "name": ["AAPL", "NVDA", "MSFT"],
            "price": [175.5, None, 0.0],
            "relative_strength_index_14": [35.2, 47.6, 41.0],
            "average_directional_index_14": [25.6, 40.1, 22.0],
            "volume": [75_000_000.0, 44_000_000.0, 31_000_000.0],
            "change_percent": [1.2, 0.2, -0.1],
            "country": ["United States", "United States", "United States"],
        }
    )

    with (
        patch(
            "app.mcp_server.tooling.screening.us._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.us.TvScreenerService",
            return_value=service,
        ),
    ):
        result = await _screen_us_via_tvscreener(limit=5)

    assert result["count"] == 1
    assert [stock["symbol"] for stock in result["stocks"]] == ["AAPL"]
    assert result["stocks"][0]["price"] == 175.5


@pytest.mark.asyncio
async def test_screen_kr_falls_back_to_name_when_description_missing(
    normalized_kr_df: pd.DataFrame,
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    df = normalized_kr_df.copy()
    df.loc[0, "description"] = None
    service.query_stock_screener.return_value = df.iloc[[0]]

    async def mock_fetch_stock_all_cached(market: str):
        if market == "STK":
            return [
                {
                    "code": "005930",
                    "short_code": "005930",
                    "name": "삼성전자",
                    "market": "KOSPI",
                    "market_cap": 4800000,
                }
            ]
        return []

    with (
        patch(
            "app.mcp_server.tooling.screening.kr._import_tvscreener",
            return_value=fake_tvscreener_module,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.TvScreenerService",
            return_value=service,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.fetch_stock_all_cached",
            side_effect=mock_fetch_stock_all_cached,
        ),
        patch(
            "app.mcp_server.tooling.screening.kr.fetch_valuation_all_cached",
            AsyncMock(return_value={}),
        ),
    ):
        result = await _screen_kr_via_tvscreener(limit=1)

    assert result["stocks"][0]["name"] == "005930"


class TestStockScreeningIntegration:
    @pytest.mark.integration
    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_kr_screening_real_api(self) -> None:
        pytest.importorskip("tvscreener")

        result = await _screen_kr_via_tvscreener(limit=5)

        assert result["source"] == "tvscreener"
        if result["error"] is not None and "KRX session expired" in result["error"]:
            pytest.skip("KRX universe unavailable for live tvscreener integration test")
        assert result["error"] is None
        if result["stocks"]:
            first = result["stocks"][0]
            assert ":" not in first["symbol"]
            assert first["name"]

    @pytest.mark.integration
    @pytest.mark.live
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


class TestNormalizeScreenRequestUsSectorCanonicalization:
    """normalize_screen_request must title-case US sector labels."""

    def test_us_category_lowercase_technology_becomes_title_case(self) -> None:
        result = normalize_screen_request(
            market="us",
            asset_type=None,
            category="technology",
            sector=None,
            strategy=None,
            sort_by=None,
            sort_order=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_dividend=None,
            min_analyst_buy=None,
            max_rsi=None,
            limit=10,
        )
        assert result["sector"] == "Technology"

    def test_us_category_lowercase_acronym_ai_becomes_uppercase(self) -> None:
        result = normalize_screen_request(
            market="us",
            asset_type=None,
            category="ai",
            sector=None,
            strategy=None,
            sort_by=None,
            sort_order=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_dividend=None,
            min_analyst_buy=None,
            max_rsi=None,
            limit=10,
        )
        assert result["sector"] == "AI"
