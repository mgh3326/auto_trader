# tests/test_screener_service_profitable_company.py
from __future__ import annotations

import datetime as dt

import pytest

from app.services.invest_view_model import screener_service
from app.services.invest_view_model.fundamentals_screener import (
    FundamentalsScreenResult,
)

# ROB-428 PR-B: the 7 FUNDAMENTALS_PRESET_SPECS presets now read the
# tvscreener-backed KR snapshot (invest_kr_fundamentals_snapshots) on the KR
# display read-path, not the DART market_valuation/financial_fundamentals tables.
# These tests therefore monkeypatch the new loader and assert the new source.
_TV_LOADER_PATH = (
    "app.services.invest_view_model.kr_fundamentals_tv_screener."
    "load_kr_fundamentals_preset_from_tv_snapshot"
)
_FUNDAMENTALS_SOURCE = "invest_kr_fundamentals_snapshots"


class _StubScreening:
    async def list_screening(self, **kwargs):  # must never be called for this preset
        raise AssertionError("profitable_company must be snapshot-only")


class _MockRelation:
    def __init__(self):
        self.has_position = False
        self.is_interested = False
        self.interest_id = None
        self.quantity = None


class _MockResolver:
    def relation(self, market, symbol):
        return _MockRelation()


class _MockResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _MockSession:
    async def execute(self, stmt):
        return _MockResult()


@pytest.mark.asyncio
async def test_profitable_company_uses_fundamentals_loader_and_is_snapshot_only(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _fake_loader(session, *, market, spec, limit, now):
        return FundamentalsScreenResult(
            rows=[
                {
                    "symbol": "005930",
                    "market": "kr",
                    "name": "삼성전자",
                    "close": 78000.0,
                    "change_rate": 1.2,
                    "volume": 12_345_678.0,
                    "market_cap": 470_000_000_000_000.0,
                    "category": "Semiconductors",
                    "roe": 20.0,
                    "gross_margin_ttm": 0.31,
                    "snapshot_date": dt.date(2026, 6, 4),
                    "_screener_snapshot_state": "fresh",
                }
            ],
            valuation_partition_date=dt.date(2026, 6, 4),
            fundamentals_partition_date=dt.date(2026, 6, 4),
            fundamentals_collected_at=dt.datetime(2026, 6, 4, tzinfo=dt.UTC),
            fundamentals_state="fresh",
        )

    monkeypatch.setattr(_TV_LOADER_PATH, _fake_loader)
    result = await screener_service.build_screener_results(
        preset_id="profitable_company",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert [row.symbol for row in result.results] == ["005930"]
    # filled row fields come straight from the tvscreener snapshot
    row = result.results[0]
    assert row.name == "삼성전자"
    assert row.category == "Semiconductors"
    assert row.priceLabel != "-"
    assert row.metricValueLabel == "20.0%"  # profitable_company metric=roe
    assert result.freshness.primary.source == _FUNDAMENTALS_SOURCE
    deps = {d.kind: d for d in result.freshness.dependencies}
    assert "fundamentals" in deps
    assert deps["fundamentals"].source == _FUNDAMENTALS_SOURCE


@pytest.mark.asyncio
async def test_profitable_company_missing_when_loader_returns_none(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _none_loader(session, *, market, spec, limit, now):
        return None

    monkeypatch.setattr(_TV_LOADER_PATH, _none_loader)
    result = await screener_service.build_screener_results(
        preset_id="profitable_company",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert result.results == []
    assert result.freshness.overallState == "missing"


@pytest.mark.asyncio
async def test_stable_growth_routes_to_fundamentals_loader(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )
    captured = {}

    async def _fake_loader(session, *, market, spec, limit, now):
        captured["preset_id"] = spec.preset_id
        return FundamentalsScreenResult(
            rows=[],
            valuation_partition_date=dt.date(2026, 6, 4),
            fundamentals_partition_date=None,
            fundamentals_collected_at=None,
            fundamentals_state="missing",
        )

    monkeypatch.setattr(_TV_LOADER_PATH, _fake_loader)
    result = await screener_service.build_screener_results(
        preset_id="stable_growth",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert captured["preset_id"] == "stable_growth"  # registry routed the right spec
    assert result.freshness.primary.source == _FUNDAMENTALS_SOURCE
    assert "fundamentals" in {d.kind for d in result.freshness.dependencies}


@pytest.mark.asyncio
async def test_steady_dividend_surfaces_streak_skip_warning(monkeypatch):
    """ROB-428 PR-B: the honest earnings-streak skip warning must reach the
    response warnings when the loader returns it."""
    from app.services.invest_view_model.kr_fundamentals_tv_screener import (
        EARNINGS_STREAK_SKIP_WARNING,
    )

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _fake_loader(session, *, market, spec, limit, now):
        return FundamentalsScreenResult(
            rows=[
                {
                    "symbol": "005930",
                    "market": "kr",
                    "name": "삼성전자",
                    "close": 78000.0,
                    "dividend_yield": 3.5,
                    "snapshot_date": dt.date(2026, 6, 4),
                    "_screener_snapshot_state": "fresh",
                }
            ],
            valuation_partition_date=dt.date(2026, 6, 4),
            fundamentals_partition_date=dt.date(2026, 6, 4),
            fundamentals_collected_at=dt.datetime(2026, 6, 4, tzinfo=dt.UTC),
            fundamentals_state="fresh",
            warnings=[EARNINGS_STREAK_SKIP_WARNING],
        )

    monkeypatch.setattr(_TV_LOADER_PATH, _fake_loader)
    result = await screener_service.build_screener_results(
        preset_id="steady_dividend",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert [row.symbol for row in result.results] == ["005930"]
    assert EARNINGS_STREAK_SKIP_WARNING in result.warnings


@pytest.mark.asyncio
async def test_cheap_value_empty_fundamentals_surfaces_missing_dependency(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _empty_fundamentals_loader(session, *, market, spec, limit, now):
        # partition exists, but no qualifying rows (Path B → missing dependency).
        return FundamentalsScreenResult(
            rows=[],
            valuation_partition_date=dt.date(2026, 6, 4),
            fundamentals_partition_date=None,
            fundamentals_collected_at=None,
            fundamentals_state="missing",
        )

    monkeypatch.setattr(_TV_LOADER_PATH, _empty_fundamentals_loader)
    result = await screener_service.build_screener_results(
        preset_id="cheap_value",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert result.results == []
    fundamentals_deps = [
        d for d in result.freshness.dependencies if d.kind == "fundamentals"
    ]
    assert fundamentals_deps and fundamentals_deps[0].dataState == "missing"
    assert fundamentals_deps[0].source == _FUNDAMENTALS_SOURCE


@pytest.mark.asyncio
async def test_undervalued_breakout_routes_snapshot_only_no_fundamentals_dependency(
    monkeypatch,
):
    # ROB-428 PR-B leaves undervalued_breakout on its CURRENT (market_valuation)
    # loader — it is NOT one of the 7 rerouted presets.
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _fake_loader(session, *, market, limit, today_market_date=None):
        return [
            {
                "symbol": "907001",
                "market": "kr",
                "name": "종목907001",
                "per": 8.0,
                "pbr": 0.8,
                "high_52w": 100.0,
                "high_52w_proximity": 0.96,
                "latest_close": 96.0,
                "snapshot_date": dt.date(2026, 6, 4),
                "_screener_snapshot_state": "fresh",
            }
        ]

    monkeypatch.setattr(
        "app.services.invest_view_model.undervalued_breakout_screener.load_undervalued_breakout_from_snapshots",
        _fake_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="undervalued_breakout",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert [r.symbol for r in result.results] == ["907001"]
    assert result.freshness.primary.source == "market_valuation_snapshots"
    # valuation-only: NO fundamentals dependency attached
    assert "fundamentals" not in {d.kind for d in result.freshness.dependencies}


@pytest.mark.asyncio
async def test_undervalued_breakout_missing_when_loader_none(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _none_loader(session, *, market, limit, today_market_date=None):
        return None

    monkeypatch.setattr(
        "app.services.invest_view_model.undervalued_breakout_screener.load_undervalued_breakout_from_snapshots",
        _none_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="undervalued_breakout",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert result.results == []
    assert result.freshness.overallState == "missing"
