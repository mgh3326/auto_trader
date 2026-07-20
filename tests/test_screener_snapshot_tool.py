"""ROB-439 PR3: screen_stocks_snapshot MCP tool (filters-over-snapshot).

Unit tests with build_screener_results + session factory monkeypatched (no DB):
filter parsing → conditions, catalog exposure, threading, fail-soft on bad input.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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
async def test_support_proximity_preset_dispatches_and_warns_on_filters(
    patched,
) -> None:
    """ROB-976: preset='support_proximity' reaches build_screener_results (MCP
    exposure, AC2/AC4) and — since it has no adjustable-filter catalog wired
    (same as cheap_value) — filters= produces the honest '필터 미적용' warning
    rather than silently dropping them."""
    out = await tool.screen_stocks_snapshot_impl(
        preset="support_proximity",
        market="kr",
        filters=[{"field": "per", "operator": "lte", "value": 8}],
    )
    assert patched["preset_id"] == "support_proximity"
    assert out["snapshotKind"] is None
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
        *,
        rows: list[dict[str, Any]],
        market: str,
        session_factory,
        opinion_provider=None,
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

    async def _fake_collect_kis_positions(
        market_filter: str | None, *, is_mock: bool = False
    ):
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
async def test_snapshot_tool_marks_us_kis_live_held_rows(monkeypatch) -> None:
    class _HeldResp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "high_yield_value",
                "results": [
                    {
                        "rank": 1,
                        "symbol": "BRK.B",
                        "market": "us",
                        "name": "Berkshire Hathaway",
                        "isWatched": False,
                        "isHeld": True,
                    }
                ],
                "warnings": [],
            }

    async def _fake_build(**kwargs: Any) -> _HeldResp:
        resolver = kwargs["resolver"]
        assert resolver.relation("us", "BRK/B") == "held"
        return _HeldResp()

    async def _fake_collect_kis_positions(
        market_filter: str | None, *, is_mock: bool = False
    ):
        assert market_filter == "equity_us"
        assert is_mock is False
        return ([{"market": "us", "symbol": "BRK.B"}], [])

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
        preset="high_yield_value",
        market="us",
        limit=10,
    )

    assert out["results"][0]["isHeld"] is True
    assert out["holdings"]["held_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_holdings_failure_warns_and_keeps_results(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"rank": 1, "symbol": "005930", "market": "kr"}],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    async def _fail_collect_kis_positions(
        market_filter: str | None, *, is_mock: bool = False
    ):
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
async def test_snapshot_tool_holdings_error_tuple_warns_and_keeps_results(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"rank": 1, "symbol": "005930", "market": "kr"}],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    async def _collect_with_errors(market_filter: str | None, *, is_mock: bool = False):
        assert market_filter == "equity_kr"
        assert is_mock is False
        return (
            [],
            [{"source": "kis", "market": "kr", "error": "token expired"}],
        )

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
        _collect_with_errors,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        exclude_held=True,
    )

    assert out["results"][0]["symbol"] == "005930"
    assert out["holdings"]["status"] == "error"
    assert out["holdings"]["held_count"] == 0
    assert out["holdings"]["warning_count"] == 1
    assert any("KIS live 보유종목 확인 실패" in w for w in out["warnings"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_holdings_partial_tuple_warns_and_marks_rows(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"rank": 1, "symbol": "005930", "market": "kr"},
                    {"rank": 2, "symbol": "000660", "market": "kr"},
                ],
                "warnings": [],
            }

    async def _fake_build(**kwargs: Any) -> _Resp:
        resolver = kwargs["resolver"]
        assert resolver.relation("kr", "005930") == "held"
        assert resolver.relation("kr", "000660") == "none"
        return _Resp()

    async def _collect_partial(market_filter: str | None, *, is_mock: bool = False):
        assert market_filter == "equity_kr"
        assert is_mock is False
        return (
            [{"symbol": "005930", "market": "kr"}],
            [{"source": "kis", "market": "us", "error": "temporary failure"}],
        )

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
        _collect_partial,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
    )

    assert out["holdings"]["status"] == "partial"
    assert out["holdings"]["held_count"] == 1
    assert out["holdings"]["warning_count"] == 1
    assert any("KIS live 보유종목 확인 실패" in w for w in out["warnings"])


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_accepts_presets_list_for_multi_sweep(monkeypatch) -> None:
    async def _fake_build(**kwargs: Any) -> Any:
        pid = kwargs["preset_id"]
        rows = [{"symbol": "S1"}] if pid == "preset_a" else [{"symbol": "S2"}]
        return MagicMock(
            model_dump=lambda mode=None: {
                "presetId": pid,
                "results": rows,
                "warnings": [],
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
        presets=["preset_a", "preset_b"],
        market="kr",
    )

    assert [r["symbol"] for r in out["results"]] == ["S1", "S2"]
    assert out["presetId"] == "multi"
    assert out["presets"] == ["preset_a", "preset_b"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_multi_preset_pagination_counts_merged_symbols(
    monkeypatch,
) -> None:
    async def _fake_build(**kwargs: Any) -> Any:
        pid = kwargs["preset_id"]
        rows = (
            [{"symbol": "S1"}, {"symbol": "S2"}, {"symbol": "S3"}]
            if pid == "A"
            else [{"symbol": "S2"}, {"symbol": "S4"}]
        )
        return MagicMock(
            model_dump=lambda mode=None: {
                "presetId": pid,
                "results": rows,
                "warnings": [],
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
        preset="A,B",
        market="kr",
        limit=2,
        offset=1,
    )

    assert [r["symbol"] for r in out["results"]] == ["S2", "S3"]
    assert out["pagination"]["total_available"] == 4
    assert out["pagination"]["returned_count"] == 2
    assert out["pagination"]["next_offset"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_filters_exclude_watched_held(monkeypatch) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "S1", "isWatched": True, "isHeld": False},
                    {"symbol": "S2", "isWatched": False, "isHeld": True},
                    {"symbol": "S3", "isWatched": False, "isHeld": False},
                ],
                "warnings": [],
            }

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )

    async def _fake_build_async(**kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build_async,
    )

    # Exclude watched -> S1 gone
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", exclude_watched=True
    )
    assert [r["symbol"] for r in out["results"]] == ["S2", "S3"]

    # Exclude held -> S2 gone
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", exclude_held=True
    )
    assert [r["symbol"] for r in out["results"]] == ["S1", "S3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_reports_excluded_held_count(monkeypatch) -> None:
    """ROB-543 Slice B: the count of rows dropped by exclude_held is surfaced as
    excluded_held_count (0 when exclude_held is off)."""

    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "S1", "isWatched": False, "isHeld": True},
                    {"symbol": "S2", "isWatched": False, "isHeld": True},
                    {"symbol": "S3", "isWatched": False, "isHeld": False},
                ],
                "warnings": [],
            }

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )

    async def _fake_build_async(**kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build_async,
    )

    # exclude_held=True drops the two held rows → count == 2
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", exclude_held=True
    )
    assert [r["symbol"] for r in out["results"]] == ["S3"]
    assert out["excluded_held_count"] == 2
    assert out["discoveryFilters"]["exclude_held"] is True

    # exclude_held=False (default) → no rows dropped → count == 0
    out2 = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr"
    )
    assert out2["excluded_held_count"] == 0
    assert len(out2["results"]) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_exclude_watched_warns_unsupported_in_mcp(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [{"symbol": "S1", "isWatched": False, "isHeld": False}],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        exclude_watched=True,
    )

    assert out["results"][0]["symbol"] == "S1"
    assert out["results"][0]["isWatched"] is False
    assert any(
        "exclude_watched" in w and "지원하지 않습니다" in w for w in out["warnings"]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_excludes_explicit_symbols(monkeypatch) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "005930", "market": "kr"},
                    {"symbol": "000660", "market": "kr"},
                ],
                "warnings": [],
            }

    async def _fake_build_async(**kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build_async,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        exclude_symbols=[" 005930 "],
    )

    assert [r["symbol"] for r in out["results"]] == ["000660"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_filters_market_cap_and_analyst(monkeypatch) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "S1", "marketCapValue": 500_000_000_000.0},  # 5000억
                    {"symbol": "S2", "marketCapValue": 200_000_000_000.0},  # 2000억
                ],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    async def _fake_enrich_page(*, rows: list[dict[str, Any]], **kwargs: Any):
        # S1 has 2 buy ratings, S2 has 0
        enriched = []
        for r in rows:
            buy_count = 2 if r["symbol"] == "S1" else 0
            total_count = 2 if r["symbol"] == "S1" else 1
            enriched.append(
                {
                    **r,
                    "analysisContext": {
                        "consensus": {
                            "buyCount": buy_count,
                            "totalCount": total_count,
                        }
                    },
                }
            )
        return {"results": enriched, "summary": {"warnings": []}}

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    # Min market cap 3000억 -> S2 gone
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", min_market_cap_eok=3000
    )
    assert [r["symbol"] for r in out["results"]] == ["S1"]

    # ROB-686: min_analyst_* now resolves counts via the cache-aside resolver
    # BEFORE enrichment, so stub resolve_consensus_counts directly (the
    # _fake_enrich_page consensus stub above is no longer the filter input).
    async def _fake_counts(*, symbols, market, redis_client=None, memo=None, **kw):
        return {
            "S1": {"totalCount": 2, "buyCount": 2},
            "S2": {"totalCount": 1, "buyCount": 0},
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _fake_counts,
    )

    # Min analyst buy 1 -> S2 gone
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", min_analyst_buy_count=1
    )
    assert [r["symbol"] for r in out["results"]] == ["S1"]

    # Raw numeric min market cap uses marketCapValue units directly.
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", min_market_cap=300_000_000_000.0
    )
    assert [r["symbol"] for r in out["results"]] == ["S1"]

    # Total analyst coverage can pass even when buyCount is 0.
    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", min_analyst_count=1
    )
    assert [r["symbol"] for r in out["results"]] == ["S1", "S2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_min_market_cap_warns_for_missing_market_cap(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "S1", "marketCapValue": 500_000_000_000.0},
                    {"symbol": "S2", "marketCapValue": None},
                    {"symbol": "S3"},
                ],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        min_market_cap_eok=3000,
    )

    assert [r["symbol"] for r in out["results"]] == ["S1"]
    assert any("marketCapValue 결측 2개 행" in w for w in out["warnings"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_sorts_by_matched_presets_desc(monkeypatch) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": "S1", "matchedPresets": ["A"]},
                    {"symbol": "S2", "matchedPresets": ["A", "B"]},  # Intersection
                    {"symbol": "S3", "matchedPresets": ["A"]},
                ],
                "warnings": [],
            }

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )

    async def _fake_build_sort(**kwargs: Any) -> _Resp:
        pid = kwargs["preset_id"]
        # S2 is in both A and B, S1 only in A, S3 only in B
        if pid == "A":
            rows = [{"symbol": "S1"}, {"symbol": "S2"}]
        else:
            rows = [{"symbol": "S2"}, {"symbol": "S3"}]

        class _DynamicResp:
            def model_dump(self, mode: str | None = None) -> dict[str, Any]:
                return {"presetId": pid, "results": rows, "warnings": []}

        return _DynamicResp()

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build_sort,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="A,B", market="kr", sort="matched_presets_desc"
    )
    # S2 should be first (matched 2 presets)
    assert out["results"][0]["symbol"] == "S2"
    assert len(out["results"][0]["matchedPresets"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_rejects_too_many_presets() -> None:
    out = await tool.screen_stocks_snapshot_impl(
        presets=["p1", "p2", "p3", "p4", "p5", "p6"],
        market="kr",
    )

    assert "error" in out
    assert "maximum is 5" in out["error"]
    assert out["results"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_tool_analyst_filter_rejects_large_unpaged_enrichment(
    monkeypatch,
) -> None:
    class _Resp:
        def model_dump(self, mode: str | None = None) -> dict[str, Any]:  # noqa: ARG002
            return {
                "presetId": "consecutive_gainers",
                "results": [
                    {"symbol": f"S{i}", "marketCapValue": 1.0} for i in range(201)
                ],
                "warnings": [],
            }

    async def _fake_build(**_kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(tool, "_session_factory", lambda: lambda: _FakeCM())
    monkeypatch.setattr(
        "app.services.screener_service.ScreenerService", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service.build_screener_results",
        _fake_build,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        min_analyst_count=1,
    )

    assert "error" in out
    assert "analyst enrichment row cap" in out["error"]
    assert out["results"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_min_analyst_filters_via_counts_and_enriches_only_page(
    monkeypatch,
) -> None:
    _patch_build_with_n_results(monkeypatch, 5)  # symbols S0..S4

    async def _fake_counts(*, symbols, market, redis_client=None, memo=None, **kw):
        # S0,S1,S2 qualify (>=3), S3,S4 do not
        return {
            s: {"totalCount": (3 if i < 3 else 1), "buyCount": (2 if i < 3 else 0)}
            for i, s in enumerate(symbols)
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _fake_counts,
    )

    enriched_symbols: list[list[str]] = []

    async def _fake_enrich_page(
        *, rows, market, session_factory, opinion_provider=None
    ):
        enriched_symbols.append([r["symbol"] for r in rows])
        return {
            "results": [
                {**r, "analystLabel": "x", "analysisContext": {}} for r in rows
            ],
            "summary": {"attempted": len(rows), "warnings": []},
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers",
        market="kr",
        min_analyst_count=3,
        limit=2,
        offset=0,
    )
    # 3 qualified, page of 2
    assert out["pagination"]["total_available"] == 3
    assert len(out["results"]) == 2
    # enrichment saw ONLY the 2 returned page rows, not all matched/qualified rows
    assert enriched_symbols == [["S0", "S1"]]
