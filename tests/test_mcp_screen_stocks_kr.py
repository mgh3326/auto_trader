# pyright: reportMissingImports=false
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_screening
from app.mcp_server.tooling.screening import kr as kr_mod
from app.mcp_server.tooling.screening import kr as kr_screening
from app.mcp_server.tooling.screening.kr_ranking_snapshot import KrRankingSnapshotResult
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


@pytest.fixture(autouse=True)
def mock_disable_kr_ranking_snapshot_by_default():
    with patch(
        "app.mcp_server.tooling.screening.kr.load_kr_ranking_snapshot",
        new=AsyncMock(return_value=None),
    ):
        yield


def test_analysis_screening_reexports_screen_contract_helpers() -> None:
    assert callable(analysis_screening.screen_stocks_unified)
    assert callable(analysis_screening.normalize_screen_request)
    assert callable(analysis_screening.build_screen_response)
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
    assert called["min_dividend"] == pytest.approx(3.0)
    first = result["results"][0]
    assert first["sector"] == "반도체"
    assert first["analyst_buy"] == 16
    assert first["analyst_hold"] == 2
    assert first["analyst_sell"] == 0
    assert first["avg_target"] == pytest.approx(98000.0)
    assert first["upside_pct"] == pytest.approx(18.7)


@pytest.mark.asyncio
async def test_screen_stocks_crypto_strategy_default_uses_trade_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()
    called: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        return {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": {"market": kwargs["market"]},
            "market": kwargs["market"],
            "timestamp": "2026-05-06T00:00:00Z",
        }

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    await tools["screen_stocks"](market="crypto", strategy="oversold", limit=5)

    assert called["market"] == "crypto"
    assert called["max_rsi"] == pytest.approx(30.0)
    assert called["sort_by"] == "trade_amount"
    assert called["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_screen_stocks_crypto_strategy_preserves_explicit_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()
    called: dict[str, Any] = {}

    async def fake_screen(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        return {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": {"market": kwargs["market"]},
            "market": kwargs["market"],
            "timestamp": "2026-05-06T00:00:00Z",
        }

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    await tools["screen_stocks"](
        market="crypto",
        strategy="oversold",
        sort_by="rsi",
        sort_order="asc",
        limit=5,
    )

    assert called["market"] == "crypto"
    assert called["max_rsi"] == pytest.approx(30.0)
    assert called["sort_by"] == "rsi"
    assert called["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_normalize_kr_results_prefers_krx_canonical_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_stock_all_cached(*, market: str) -> list[dict[str, Any]]:
        return [
            {
                "code": "005930",
                "short_code": "005930",
                "name": "삼성전자",
                "market": market,
            }
        ]

    async def fake_fetch_valuation_all_cached(
        *, market: str
    ) -> dict[str, dict[str, Any]]:
        return {}

    monkeypatch.setattr(
        kr_screening, "fetch_stock_all_cached", fake_fetch_stock_all_cached
    )
    monkeypatch.setattr(
        kr_screening, "fetch_valuation_all_cached", fake_fetch_valuation_all_cached
    )

    df = pd.DataFrame(
        [
            {
                "symbol": "KRX:005930",
                "name": "Samsung Electronics",
                "description": "Samsung Electronics",
                "price": 80_000,
            }
        ]
    )

    rows = await kr_screening._normalize_kr_results(df, market="kr")

    assert rows[0]["name"] == "삼성전자"
    assert rows[0]["instrument_type"] == "common"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fallback_uses_snapshot_when_available():
    snap = KrRankingSnapshotResult(
        rows=[
            {"symbol": "005930", "name": "삼성전자", "change_rate": 3.5, "market": "kr"}
        ],
        total_count=1,
        data_state="fresh",
        source="kr_market_ranking",
        latest_snapshot_at="2026-06-08T00:00:00+00:00",
        warnings=["모멘텀 랭킹 상위 1종목 기반 — 전체 KRX 스캔이 아닙니다."],
        meta_fields={"data_state": "fresh", "source": "kr_market_ranking"},
    )
    with patch.object(
        kr_mod, "load_kr_ranking_snapshot", new=AsyncMock(return_value=snap)
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr",
            asset_type=None,
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="desc",
            limit=20,
        )
    assert resp["meta"]["data_state"] == "fresh"
    assert resp["meta"]["source"] == "kr_market_ranking"
    assert resp["results"][0]["symbol"] == "005930"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fallback_stale_snapshot_returned_not_hard_zero():
    snap = KrRankingSnapshotResult(
        rows=[{"symbol": "005930", "name": "삼성", "market": "kr"}],
        total_count=1,
        data_state="stale",
        source="kr_market_ranking",
        latest_snapshot_at=None,
        warnings=[
            "모멘텀 랭킹 스냅샷이 오래되었습니다(older_than_ttl) — 신규 후보 발굴에 주의하세요."
        ],
        meta_fields={
            "data_state": "stale",
            "source": "kr_market_ranking",
            "retryable": False,
            "reason": "kr_market_ranking_stale",
        },
    )
    with patch.object(
        kr_mod, "load_kr_ranking_snapshot", new=AsyncMock(return_value=snap)
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr",
            asset_type=None,
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )
    assert resp["meta"]["data_state"] == "stale"
    assert len(resp["results"]) == 1  # NOT hard-0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fallback_none_snapshot_goes_live():
    """When the snapshot helper returns None (ineligible sort / zero rows / error),
    the legacy live path must still run (here it returns an empty legacy response)."""
    with (
        patch.object(
            kr_mod, "load_kr_ranking_snapshot", new=AsyncMock(return_value=None)
        ),
        patch.object(
            kr_mod,
            "_get_tvscreener_stock_capability_snapshot",
            new=AsyncMock(return_value={}),
        ),
        patch.object(kr_mod, "_can_use_tvscreener_stock_path", return_value=False),
        patch.object(
            kr_mod,
            "_screen_kr",
            new=AsyncMock(return_value={"meta": {"source": "legacy"}, "results": []}),
        ),
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr",
            asset_type=None,
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=20,
        )
    assert resp["meta"]["source"] == "legacy"  # fell through to live


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fallback_skips_snapshot_when_filtered():
    """A quality filter (max_per) cannot be honored by the KR-wide ranking snapshot,
    so the guard must skip the snapshot path entirely and go live (which honors it)."""
    snap_mock = AsyncMock(
        return_value=KrRankingSnapshotResult(
            rows=[{"symbol": "X"}],
            total_count=1,
            data_state="fresh",
            source="kr_market_ranking",
            latest_snapshot_at=None,
        )
    )
    with (
        patch.object(kr_mod, "load_kr_ranking_snapshot", new=snap_mock),
        patch.object(
            kr_mod,
            "_get_tvscreener_stock_capability_snapshot",
            new=AsyncMock(return_value={}),
        ),
        patch.object(kr_mod, "_can_use_tvscreener_stock_path", return_value=False),
        patch.object(
            kr_mod,
            "_screen_kr",
            new=AsyncMock(return_value={"meta": {"source": "legacy"}, "results": []}),
        ),
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kr",
            asset_type=None,
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=10.0,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )
    snap_mock.assert_not_awaited()  # guard prevented the snapshot path
    assert resp["meta"]["source"] == "legacy"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fallback_skips_snapshot_for_submarket():
    """market=kospi would be mislabeled by the KR-wide snapshot -> guard goes live."""
    snap_mock = AsyncMock(return_value=None)
    with (
        patch.object(kr_mod, "load_kr_ranking_snapshot", new=snap_mock),
        patch.object(
            kr_mod,
            "_get_tvscreener_stock_capability_snapshot",
            new=AsyncMock(return_value={}),
        ),
        patch.object(kr_mod, "_can_use_tvscreener_stock_path", return_value=False),
        patch.object(
            kr_mod,
            "_screen_kr",
            new=AsyncMock(return_value={"meta": {"source": "legacy"}, "results": []}),
        ),
    ):
        resp = await kr_mod._screen_kr_with_fallback(
            market="kospi",
            asset_type=None,
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )
    snap_mock.assert_not_awaited()  # sub-market -> live path
    assert resp["meta"]["source"] == "legacy"
