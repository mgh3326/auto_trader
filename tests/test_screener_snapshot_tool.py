"""ROB-439 PR3: screen_stocks_snapshot MCP tool (filters-over-snapshot).

Unit tests with build_screener_results + session factory monkeypatched (no DB):
filter parsing → conditions, catalog exposure, threading, fail-soft on bad input.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp_server.tooling import screener_snapshot_tool as tool


class _StubResp:
    def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
        return {"presetId": "consecutive_gainers", "results": [], "warnings": ["base"]}


class _FakeCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def patched(monkeypatch):
    """Patch the session factory, ScreenerService, and build_screener_results."""
    captured: dict[str, Any] = {}

    async def _fake_build(**kwargs: Any) -> _StubResp:
        captured.clear()
        captured.update(kwargs)
        return _StubResp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )
    return captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_threads_filters_and_exposes_catalog(patched) -> None:
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        filters=[{"field": "consecutive_up_days", "operator": "gte", "value": 3}],
    )
    # filter parsed into a condition and threaded to build_screener_results
    overrides = patched["filter_overrides"]
    assert overrides is not None and len(overrides) == 1
    assert overrides[0].field == "consecutive_up_days"
    assert overrides[0].operator == "gte"
    assert overrides[0].value == 3
    assert patched["preset_id"] == "consecutive_gainers"
    # response echoes applied filters + exposes the adjustable catalog
    assert out["appliedFilters"] == [
        {"field": "consecutive_up_days", "operator": "gte", "value": 3}
    ]
    assert "consecutive_up_days" in out["availableFilters"]
    assert out["availableFilters"]["consecutive_up_days"]["label"] == "연속상승일"
    assert out["snapshotKind"] == "invest_screener_snapshots"
    assert out["results"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_filters_passes_none_overrides(patched) -> None:
    await tool.screen_stocks_snapshot_impl(preset="consecutive_gainers", market="kr")
    assert patched["filter_overrides"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unwired_preset_with_filters_warns(patched) -> None:
    out = await tool.screen_stocks_snapshot_impl(
        preset="cheap_value",  # catalogued but no snapshot_kind in the MVP pilot
        market="kr",
        filters=[{"field": "per", "operator": "lte", "value": 8}],
    )
    assert out["snapshotKind"] is None
    assert out["availableFilters"] == {}
    assert any("배선되지 않" in w for w in out["warnings"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_high_yield_value_with_filters_warns(patched) -> None:
    # ROB-445: high_yield_value HAS a snapshot_kind (market_valuation_snapshots) but
    # build_screener_results does NOT thread its filters → it must still warn (the old
    # `snapshot_kind is None` guard let this slip through = silent no-op).
    out = await tool.screen_stocks_snapshot_impl(
        preset="high_yield_value",
        market="kr",
        filters=[{"field": "per", "operator": "lte", "value": 8}],
    )
    assert out["snapshotKind"] is not None  # has a catalog, unlike cheap_value
    assert any("배선되지 않" in w for w in out["warnings"])
    # filters were still echoed (transparency) even though not applied
    assert out["appliedFilters"] == [{"field": "per", "operator": "lte", "value": 8}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_threaded_preset_does_not_double_warn(patched) -> None:
    # ROB-445 guard: consecutive_gainers DOES thread filters → no '미적용' warning.
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        filters=[{"field": "consecutive_up_days", "operator": "gte", "value": 3}],
    )
    assert not any("배선되지 않" in w for w in out["warnings"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bad_filter_entry_fails_soft(patched) -> None:
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        filters=[{"field": "consecutive_up_days"}],  # missing operator/value
    )
    assert "error" in out
    assert out["results"] == []
    # build_screener_results must NOT have been called (no DB work on bad input)
    assert patched == {}


def _patch_build_with_n_results(monkeypatch, n: int) -> None:
    class _BigResp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"symbol": f"S{i}"} for i in range(n)],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _BigResp:
        return _BigResp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caps_results_and_paginates(monkeypatch) -> None:
    """ROB-465: a large snapshot is capped to a default page + pagination metadata."""
    _patch_build_with_n_results(monkeypatch, 100)

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr"
    )

    assert len(out["results"]) == 40  # default cap
    pg = out["pagination"]
    assert pg["total_available"] == 100
    assert pg["returned_count"] == 40
    assert pg["offset"] == 0
    assert pg["limit"] == 40
    assert pg["has_more"] is True
    assert pg["next_offset"] == 40


@pytest.mark.unit
@pytest.mark.asyncio
async def test_offset_paginates_last_partial_page(monkeypatch) -> None:
    """ROB-465: offset + limit page through; the final partial page has no more."""
    _patch_build_with_n_results(monkeypatch, 100)

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", limit=30, offset=90
    )

    assert [r["symbol"] for r in out["results"]] == [f"S{i}" for i in range(90, 100)]
    pg = out["pagination"]
    assert pg["returned_count"] == 10
    assert pg["has_more"] is False
    assert pg["next_offset"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enriches_only_returned_page(monkeypatch) -> None:
    _patch_build_with_n_results(monkeypatch, 5)
    captured: dict[str, Any] = {}

    async def _fake_enrich_page(
        *, rows: list[dict[str, Any]], market: str, session_factory
    ):
        captured["symbols"] = [row["symbol"] for row in rows]
        captured["market"] = market
        captured["session_factory"] = session_factory
        enriched = []
        for row in rows:
            enriched.append(
                {
                    **row,
                    "analystLabel": "매수 1 / 보유 0 / 매도 0 · 목표 +10.0%",
                    "analysisContext": {
                        "consensus": {
                            "source": "naver",
                            "buyCount": 1,
                            "holdCount": 0,
                            "sellCount": 0,
                            "strongBuyCount": 0,
                            "totalCount": 1,
                            "avgTargetPrice": 110.0,
                            "medianTargetPrice": 110.0,
                            "minTargetPrice": 110.0,
                            "maxTargetPrice": 110.0,
                            "upsidePct": 10.0,
                            "currentPrice": 100.0,
                        },
                        "rsi14": 58.0,
                        "dataState": "fresh",
                        "warnings": [],
                    },
                }
            )
        return {
            "results": enriched,
            "summary": {
                "attempted": len(rows),
                "consensusSucceeded": len(rows),
                "rsiSucceeded": len(rows),
                "warnings": [],
            },
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", limit=2, offset=1
    )

    assert captured["symbols"] == ["S1", "S2"]
    assert captured["market"] == "kr"
    assert len(out["results"]) == 2
    assert out["results"][0]["analysisContext"]["rsi14"] == pytest.approx(58.0)
    assert out["analysisEnrichment"]["attempted"] == 2

@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_marks_kis_live_held_rows(monkeypatch) -> None:
    class _HeldResp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {
                        "rank": 1,
                        "symbol": "005930",
                        "market": "kr",
                        "name": "삼성전자",
                        "isWatched": False,
                        "isHeld": True,
                        "matchedPresets": ["consecutive_gainers"],
                        "marketCapValue": 478_000_000_000_000.0,
                    }
                ],
                "warnings": [],
            }

    async def _fake_build(**kwargs: Any) -> _HeldResp:
        resolver = kwargs["resolver"]
        assert resolver.relation("kr", "005930") == "held"
        return _HeldResp()

    async def _fake_collect_kis_positions(market_filter: str | None, *, is_mock: bool = False):
        assert market_filter == "equity_kr"
        assert is_mock is False
        return ([{"market": "kr", "symbol": "005930"}], [])

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions",
        _fake_collect_kis_positions,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        limit=10,
    )

    assert out["results"][0]["isHeld"] is True
    assert "holdings" in out
    assert out["holdings"]["source"] == "kis_live"
    assert out["holdings"]["held_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_holdings_failure_warns_and_keeps_results(monkeypatch) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"rank": 1, "symbol": "005930", "market": "kr"}],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    async def _fail_collect_kis_positions(market_filter: str | None, *, is_mock: bool = False):
        raise RuntimeError("kis unavailable")

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.portfolio_holdings._collect_kis_positions",
        _fail_collect_kis_positions,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
    )

    assert out["results"][0]["symbol"] == "005930"
    assert any("KIS live 보유종목 확인 실패" in w for w in out["warnings"])
    assert out["holdings"]["source"] == "kis_live"
    assert out["holdings"]["status"] == "error"

@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_merges_multiple_presets(monkeypatch) -> None:
    async def _fake_build(**kwargs: Any) -> Any:
        pid = kwargs["preset_id"]
        # Mock results where S1 is in both, S2 only in A, S3 only in B
        if pid == "preset_a":
            rows = [{"symbol": "S1", "rank": 1}, {"symbol": "S2", "rank": 2}]
        else:
            rows = [{"symbol": "S1", "rank": 1}, {"symbol": "S3", "rank": 2}]

        return MagicMock(
            model_dump=lambda mode=None: {
                "presetId": pid,
                "results": rows,
                "warnings": [f"warn_{pid}"],
            }
        )

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="preset_a, preset_b",  # comma-separated string
        market="kr",
    )

    # results should contain S1, S2, S3 (deduped)
    symbols = [r["symbol"] for r in out["results"]]
    assert len(symbols) == 3
    assert "S1" in symbols
    assert "S2" in symbols
    assert "S3" in symbols

    # S1 should have both presets in its matchedPresets
    s1 = next(r for r in out["results"] if r["symbol"] == "S1")
    assert "preset_a" in s1["matchedPresets"]
    assert "preset_b" in s1["matchedPresets"]

    # warnings should be combined
    assert "warn_preset_a" in out["warnings"]
    assert "warn_preset_b" in out["warnings"]
