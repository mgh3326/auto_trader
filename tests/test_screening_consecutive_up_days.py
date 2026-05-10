"""ROB-168: post-screen OHLCV-based streak enrichment + filter."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_consecutive_up_days_uses_daily_closes() -> None:
    from app.mcp_server.tooling.screening.enrichment import (
        _enrich_consecutive_up_days,
    )

    rows: list[dict] = [
        {"symbol": "005930", "market": "kr"},
        {"symbol": "035720", "market": "kr"},
    ]

    async def fake_fetch(
        symbol: str, market_type: str, count: int = 10
    ) -> pd.DataFrame:
        if symbol == "005930":
            return pd.DataFrame({"close": [100, 101, 102, 103, 104, 105]})
        if symbol == "035720":
            return pd.DataFrame({"close": [100, 101, 100, 101, 102, 103]})
        raise AssertionError(symbol)

    with patch(
        "app.mcp_server.tooling.screening.enrichment._fetch_ohlcv_for_indicators",
        side_effect=fake_fetch,
    ):
        await _enrich_consecutive_up_days(rows, market="kr", lookback=10)

    assert rows[0]["consecutive_up_days"] == 5
    assert rows[1]["consecutive_up_days"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_consecutive_up_days_uses_kr_code_rows() -> None:
    from app.mcp_server.tooling.screening.enrichment import (
        _enrich_consecutive_up_days,
    )

    rows: list[dict] = [
        {"code": "005930", "market": "KOSPI"},
        {"code": "KRX:035720", "market": "KOSPI"},
    ]
    seen: list[str] = []

    async def fake_fetch(
        symbol: str, market_type: str, count: int = 10
    ) -> pd.DataFrame:
        seen.append(symbol)
        assert market_type == "equity_kr"
        return pd.DataFrame({"close": [100, 101, 102, 103, 104, 105]})

    with patch(
        "app.mcp_server.tooling.screening.enrichment._fetch_ohlcv_for_indicators",
        side_effect=fake_fetch,
    ):
        await _enrich_consecutive_up_days(rows, market="kr", lookback=10)

    assert seen == ["005930", "035720"]
    assert [row["consecutive_up_days"] for row in rows] == [5, 5]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrich_consecutive_up_days_tolerates_per_row_failure() -> None:
    from app.mcp_server.tooling.screening.enrichment import (
        _enrich_consecutive_up_days,
    )

    rows: list[dict] = [
        {"symbol": "BAD", "market": "kr"},
        {"symbol": "OK", "market": "kr"},
    ]

    async def fake_fetch(
        symbol: str, market_type: str, count: int = 10
    ) -> pd.DataFrame:
        if symbol == "BAD":
            raise RuntimeError("fetch failed")
        return pd.DataFrame({"close": [100, 101, 102]})

    with patch(
        "app.mcp_server.tooling.screening.enrichment._fetch_ohlcv_for_indicators",
        side_effect=fake_fetch,
    ):
        await _enrich_consecutive_up_days(rows, market="kr", lookback=10)

    assert "consecutive_up_days" not in rows[0]
    assert rows[1]["consecutive_up_days"] == 2


@pytest.mark.unit
def test_apply_min_consecutive_up_days_filter_drops_rows_below_threshold() -> None:
    from app.mcp_server.tooling.screening.common import (
        _apply_min_consecutive_up_days,
    )

    rows: list[dict] = [
        {"symbol": "A", "consecutive_up_days": 6},
        {"symbol": "B", "consecutive_up_days": 4},
        {"symbol": "C", "consecutive_up_days": None},
        {"symbol": "D"},
    ]
    out = _apply_min_consecutive_up_days(rows, threshold=5)
    assert [r["symbol"] for r in out] == ["A"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_screen_stocks_impl_overfetches_before_streak_filter() -> None:
    from app.mcp_server.tooling import analysis_tool_handlers

    requested_limits: list[int] = []

    async def fake_screen_stocks_unified(**kwargs):
        requested_limits.append(kwargs["limit"])
        rows = [
            {"code": f"{idx:06d}", "market": "KOSPI"} for idx in range(kwargs["limit"])
        ]
        return {"results": rows, "total_count": len(rows)}

    async def fake_fetch(
        symbol: str, market_type: str, count: int = 10
    ) -> pd.DataFrame:
        idx = int(symbol)
        if idx >= 20:
            return pd.DataFrame({"close": [100, 101, 102, 103, 104, 105]})
        return pd.DataFrame({"close": [100, 101, 100, 101, 100, 101]})

    with (
        patch(
            "app.mcp_server.tooling.analysis_screening.screen_stocks_unified",
            side_effect=fake_screen_stocks_unified,
        ),
        patch(
            "app.mcp_server.tooling.screening.enrichment._fetch_ohlcv_for_indicators",
            side_effect=fake_fetch,
        ),
    ):
        result = await analysis_tool_handlers.screen_stocks_impl(
            market="kr",
            asset_type="stock",
            sort_by="change_rate",
            sort_order="desc",
            min_consecutive_up_days=5,
            limit=20,
        )

    assert requested_limits == [100]
    assert len(result["results"]) == 20
    assert result["total_count"] == 80
    assert result["results"][0]["code"] == "000020"
