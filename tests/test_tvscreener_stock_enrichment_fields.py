from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.screening.kr import _screen_kr_via_tvscreener
from app.mcp_server.tooling.screening.tvscreener_support import (
    _map_tvscreener_stock_row,
)
from app.mcp_server.tooling.screening.us import _screen_us_via_tvscreener
from app.services.tvscreener_service import _normalize_result_frame


class _Condition:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return isinstance(other, _Condition) and self.label == other.label


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
            CHANGE_PERCENT=_Field("change_percent"),
            MARKET_CAPITALIZATION=_Field("market_capitalization"),
            PRICE_TO_EARNINGS_RATIO_TTM=_Field("price_to_earnings_ratio_ttm"),
            PRICE_TO_BOOK_MRQ=_Field("price_to_book_mrq"),
            DIVIDEND_YIELD_FORWARD=_Field("dividend_yield_forward"),
            SECTOR=_Field("sector"),
            RECOMMENDATION_BUY=_Field("recommendation_buy"),
            RECOMMENDATION_OVER=_Field("recommendation_over"),
            RECOMMENDATION_HOLD=_Field("recommendation_hold"),
            RECOMMENDATION_SELL=_Field("recommendation_sell"),
            RECOMMENDATION_UNDER=_Field("recommendation_under"),
            PRICE_TARGET_1Y=_Field("price_target_1y"),
            PRICE_TARGET_1Y_DELTA=_Field("price_target_1y_delta"),
            PRICE_TARGET_AVERAGE=_Field("price_target_average"),
            COUNTRY=_Field("country"),
        ),
    )


def test_normalize_result_frame_keeps_target_price_columns_accessible() -> None:
    frame = _normalize_result_frame(
        pd.DataFrame(
            {
                "Symbol": ["NASDAQ:NVDA"],
                "Name": ["NVDA"],
                "Price": [174.4],
                "Target price average": [269.16],
                "Price target 1Y delta": [54.33],
                "Sector": ["Electronic Technology"],
                "Recommendation buy": [60],
                "Recommendation over": [5],
                "Recommendation hold": [4],
                "Recommendation sell": [1],
            }
        )
    )

    row = _map_tvscreener_stock_row(frame.iloc[0].to_dict(), market="us")

    assert frame.columns.tolist() == [
        "symbol",
        "name",
        "price",
        "target_price_average",
        "price_target_1y_delta",
        "sector",
        "recommendation_buy",
        "recommendation_over",
        "recommendation_hold",
        "recommendation_sell",
    ]
    assert row["avg_target"] == pytest.approx(269.16)
    assert row["upside_pct"] == pytest.approx(54.33)
    assert row["analyst_buy"] == 65
    assert row["analyst_hold"] == 4
    assert row["analyst_sell"] == 1


@pytest.mark.asyncio
async def test_screen_us_via_tvscreener_maps_sector_analyst_and_targets(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = pd.DataFrame(
        {
            "symbol": ["NASDAQ:NVDA"],
            "description": ["NVIDIA Corporation"],
            "name": ["NVDA"],
            "price": [174.4],
            "relative_strength_index_14": [58.2],
            "average_directional_index_14": [31.6],
            "volume": [44_000_000.0],
            "change_percent": [2.1],
            "market_capitalization": [4_200_000_000_000.0],
            "price_to_earnings_ratio_ttm": [61.3],
            "dividend_yield_forward": [0.0004],
            "sector": ["Electronic Technology"],
            "recommendation_buy": [60],
            "recommendation_over": [5],
            "recommendation_hold": [4],
            "recommendation_sell": [1],
            "target_price_average": [269.16],
            "price_target_1y_delta": [54.33],
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
        result = await _screen_us_via_tvscreener(category="Electronic Technology")

    stock = result["stocks"][0]
    assert stock["sector"] == "Electronic Technology"
    assert stock["analyst_buy"] == 65
    assert stock["analyst_hold"] == 4
    assert stock["analyst_sell"] == 1
    assert stock["avg_target"] == pytest.approx(269.16)
    assert stock["upside_pct"] == pytest.approx(54.33)


@pytest.mark.asyncio
async def test_screen_kr_via_tvscreener_maps_sector_analyst_and_targets(
    fake_tvscreener_module: SimpleNamespace,
) -> None:
    service = AsyncMock()
    service.query_stock_screener.return_value = pd.DataFrame(
        {
            "symbol": ["KRX:005930"],
            "description": ["Samsung Electronics Co., Ltd."],
            "name": ["005930"],
            "price": [174.4],
            "relative_strength_index_14": [58.2],
            "average_directional_index_14": [31.6],
            "volume": [44_000_000.0],
            "change_percent": [2.1],
            "market_capitalization": [4_200_000.0],
            "price_to_earnings_ratio_ttm": [61.3],
            "price_to_book_mrq": [18.7],
            "dividend_yield_forward": [0.004],
            "sector": ["Electronic Technology"],
            "recommendation_buy": [60],
            "recommendation_over": [5],
            "recommendation_hold": [4],
            "recommendation_sell": [1],
            "target_price_average": [999.0],
            "price_target_1y": [269.16],
            "price_target_1y_delta": [54.33],
            "country": ["South Korea"],
        }
    )

    async def mock_fetch_stock_all_cached(market: str):
        assert market == "STK"
        return [
            {
                "code": "005930",
                "short_code": "005930",
                "name": "삼성전자",
                "market": "KOSPI",
                "market_cap": 4800000,
                "close": 174.4,
            }
        ]

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
        result = await _screen_kr_via_tvscreener(market="kospi", limit=5)

    stock = result["stocks"][0]
    assert stock["sector"] == "Electronic Technology"
    assert stock["analyst_buy"] == 65
    assert stock["analyst_hold"] == 4
    assert stock["analyst_sell"] == 1
    assert stock["avg_target"] == pytest.approx(269.16)
    assert stock["upside_pct"] == pytest.approx(54.33)
    assert stock["market_cap"] == pytest.approx(4_200_000.0)
    assert stock["per"] == pytest.approx(61.3)
    assert stock["pbr"] == pytest.approx(18.7)
    assert stock["dividend_yield"] == pytest.approx(0.004)
