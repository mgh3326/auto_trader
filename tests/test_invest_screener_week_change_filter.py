"""ROB-170 follow-up — week_change_rate >= 0 Toss filter."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_apply_min_week_change_rate_drops_rows_below_threshold() -> None:
    from app.mcp_server.tooling.screening.common import (
        _apply_min_week_change_rate,
    )

    rows: list[dict] = [
        {"symbol": "A", "week_change_rate": 1.5},  # keep
        {"symbol": "B", "week_change_rate": 0.0},  # keep (>= 0)
        {"symbol": "C", "week_change_rate": -0.01},  # drop
        {"symbol": "D", "week_change_rate": None},  # drop (unknown)
        {"symbol": "E"},  # drop (missing)
        {"symbol": "F", "week_change_rate": "2.3"},  # keep (string-coerced)
    ]
    out = _apply_min_week_change_rate(rows, threshold=0.0)
    assert [r["symbol"] for r in out] == ["A", "B", "F"]


@pytest.mark.unit
def test_apply_min_week_change_rate_passthrough_when_threshold_none() -> None:
    from app.mcp_server.tooling.screening.common import (
        _apply_min_week_change_rate,
    )

    rows = [{"symbol": "A", "week_change_rate": -10.0}]
    assert _apply_min_week_change_rate(rows, threshold=None) == rows


@pytest.mark.unit
def test_normalize_screen_request_rejects_non_finite_week_change_rate() -> None:
    from app.mcp_server.tooling.screening.common import normalize_screen_request

    with pytest.raises(ValueError, match="finite"):
        normalize_screen_request(
            market="kr",
            asset_type="stock",
            category=None,
            sector=None,
            strategy=None,
            sort_by=None,
            sort_order="desc",
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_dividend=None,
            min_analyst_buy=None,
            max_rsi=None,
            limit=20,
            min_week_change_rate=float("inf"),
        )


@pytest.mark.unit
def test_normalize_screen_request_accepts_zero_threshold() -> None:
    from app.mcp_server.tooling.screening.common import normalize_screen_request

    out = normalize_screen_request(
        market="kr",
        asset_type="stock",
        category=None,
        sector=None,
        strategy=None,
        sort_by=None,
        sort_order="desc",
        min_market_cap=None,
        max_per=None,
        max_pbr=None,
        min_dividend_yield=None,
        min_dividend=None,
        min_analyst_buy=None,
        max_rsi=None,
        limit=20,
        min_week_change_rate=0.0,
    )
    assert out["min_week_change_rate"] == 0.0


@pytest.mark.unit
def test_consecutive_gainers_preset_includes_week_change_filter() -> None:
    from app.services.invest_view_model.screener_presets import (
        screening_filters_for,
    )

    kr_filters = screening_filters_for("consecutive_gainers", market="kr")
    assert kr_filters["min_consecutive_up_days"] == 5
    assert kr_filters["min_week_change_rate"] == 0.0

    us_filters = screening_filters_for("consecutive_gainers", market="us")
    assert us_filters["min_consecutive_up_days"] == 5
    assert us_filters["min_week_change_rate"] == 0.0


@pytest.mark.unit
def test_sort_and_limit_supports_week_change_rate() -> None:
    from app.mcp_server.tooling.screening.common import _sort_and_limit

    rows = [
        {"symbol": "A", "week_change_rate": 1.0},
        {"symbol": "B", "week_change_rate": 8.0},
        {"symbol": "C", "week_change_rate": 3.0},
        {"symbol": "D", "week_change_rate": None},
    ]

    out = _sort_and_limit(rows, "week_change_rate", "desc", 3)

    assert [r["symbol"] for r in out] == ["B", "C", "A"]


@pytest.mark.asyncio
async def test_screen_stocks_impl_drops_negative_week_change(monkeypatch) -> None:
    from app.mcp_server.tooling import analysis_screening
    from app.mcp_server.tooling import analysis_tool_handlers as handlers

    fake_rows = [
        {
            "market": "kr",
            "code": "A",
            "consecutive_up_days": 6,
            "week_change_rate": 1.2,
        },
        {
            "market": "kr",
            "code": "B",
            "consecutive_up_days": 5,
            "week_change_rate": -0.5,
        },
        {
            "market": "kr",
            "code": "C",
            "consecutive_up_days": 7,
            "week_change_rate": None,
        },
        {
            "market": "kr",
            "code": "D",
            "consecutive_up_days": 5,
            "week_change_rate": 0.0,
        },
    ]

    async def fake_unified(**kwargs):
        return {"results": list(fake_rows), "total_count": 4, "filters_applied": {}}

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_unified)

    async def fake_enrich(rows, *, market, session=None, lookback=10):
        return None

    monkeypatch.setattr(
        "app.mcp_server.tooling.screening.enrichment._enrich_consecutive_up_days",
        fake_enrich,
    )

    result = await handlers.screen_stocks_impl(
        market="kr",
        asset_type="stock",
        sort_by="change_rate",
        sort_order="desc",
        min_consecutive_up_days=5,
        min_week_change_rate=0.0,
        limit=20,
    )
    codes = [r["code"] for r in result["results"]]
    assert codes == ["A", "D"]
