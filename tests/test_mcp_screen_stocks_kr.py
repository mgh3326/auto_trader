from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import analysis_screening
from tests._mcp_screen_stocks_support import (
    TestScreenStocksKR,
    TestScreenStocksKRRegression,
    build_tools,
    test_screen_stocks_smoke,
)

pytest_plugins = ("tests._mcp_screen_stocks_support",)

__all__ = [
    "TestScreenStocksKR",
    "TestScreenStocksKRRegression",
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
