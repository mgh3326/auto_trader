"""ROB-439 PR3: screen_stocks_snapshot MCP tool (filters-over-snapshot).

Unit tests with build_screener_results + session factory monkeypatched (no DB):
filter parsing → conditions, catalog exposure, threading, fail-soft on bad input.
"""

from __future__ import annotations

from typing import Any

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
