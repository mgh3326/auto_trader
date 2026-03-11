# pyright: reportMissingImports=false
from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import analysis_screening
from tests._mcp_screen_stocks_support import (
    TestScreenStocksFundamentalsExpansion,
    TestScreenStocksKR,
    TestScreenStocksKRRegression,
    build_tools,
    test_screen_stocks_smoke,
)

pytest_plugins = ("tests._mcp_screen_stocks_support",)

__all__ = [
    "TestScreenStocksKR",
    "TestScreenStocksKRRegression",
    "TestScreenStocksFundamentalsExpansion",
    "test_screen_stocks_smoke",
]


def test_analysis_screening_reexports_screen_contract_helpers() -> None:
    assert callable(analysis_screening.screen_stocks_unified)
    assert callable(analysis_screening._normalize_screen_market)
    assert callable(analysis_screening._normalize_asset_type)
    assert callable(analysis_screening._normalize_sort_by)
    assert callable(analysis_screening._normalize_sort_order)
    assert callable(analysis_screening._validate_screen_filters)


@pytest.mark.asyncio
async def test_screen_stocks_tool_uses_analysis_screening_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()
    called: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        market = str(kwargs["market"])
        return {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": {"market": market},
            "market": market,
            "timestamp": "2026-03-10T00:00:00Z",
            "meta": {"source": "screening-facade"},
        }

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    result = await tools["screen_stocks"](market="kr")

    assert result["meta"]["source"] == "screening-facade"
    assert called["market"] == "kr"
    assert called["limit"] == 50

@pytest.mark.asyncio
async def test_screen_stocks_tool_uses_analysis_screening_normalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()
    called: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        market = str(kwargs["market"])
        return {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": {"market": market},
            "market": market,
            "timestamp": "2026-03-10T00:00:00Z",
            "meta": {"source": "screening-normalizer"},
        }

    monkeypatch.setattr(
        analysis_screening, "_normalize_screen_market", lambda market: "kosdaq"
    )
    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    result = await tools["screen_stocks"](market="kr")

    assert result["market"] == "kosdaq"
    assert result["meta"]["source"] == "screening-normalizer"
    assert called["market"] == "kosdaq"


@pytest.mark.asyncio
async def test_screen_stocks_tool_forwards_new_fundamentals_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()
    called: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        return {
            "results": [
                {
                    "code": "005930",
                    "name": "삼성전자",
                    "sector": "반도체",
                    "analyst_buy": 16,
                    "analyst_hold": 2,
                    "analyst_sell": 0,
                    "avg_target": 98000.0,
                    "upside_pct": 18.7,
                }
            ],
            "total_count": 1,
            "returned_count": 1,
            "filters_applied": {
                "market": "kr",
                "sector": "반도체",
                "min_analyst_buy": 8,
                "min_dividend_input": 3.0,
                "min_dividend_normalized": 0.03,
            },
            "market": "kr",
            "timestamp": "2026-03-11T00:00:00Z",
            "meta": {"source": "fundamentals-forwarding"},
        }

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    result = await tools["screen_stocks"](
        market="kr",
        asset_type="stock",
        sector="반도체",
        min_analyst_buy=8,
        min_dividend=3.0,
        limit=10,
    )

    assert called["market"] == "kr"
    assert called["asset_type"] == "stock"
    assert called["sector"] == "반도체"
    assert called["min_analyst_buy"] == 8
    assert called["min_dividend"] == 3.0
    first = result["results"][0]
    assert first["sector"] == "반도체"
    assert first["analyst_buy"] == 16
    assert first["analyst_hold"] == 2
    assert first["analyst_sell"] == 0
    assert first["avg_target"] == 98000.0
    assert first["upside_pct"] == 18.7
