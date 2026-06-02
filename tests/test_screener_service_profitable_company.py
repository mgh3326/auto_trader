# tests/test_screener_service_profitable_company.py
from __future__ import annotations

import datetime as dt

import pytest

from app.services.invest_view_model import screener_service
from app.services.invest_view_model.fundamentals_screener import (
    FundamentalsScreenResult,
)


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
                    "roe": 20.0,
                    "gross_margin_ttm": 0.31,
                    "snapshot_date": dt.date(2026, 6, 2),
                    "_screener_snapshot_state": "fresh",
                }
            ],
            valuation_partition_date=dt.date(2026, 6, 2),
            fundamentals_partition_date=dt.date(2025, 12, 31),
            fundamentals_collected_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
            fundamentals_state="fresh",
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _fake_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="profitable_company",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert [row.symbol for row in result.results] == ["005930"]
    assert result.freshness.primary.source == "market_valuation_snapshots"
    dep_kinds = {d.kind for d in result.freshness.dependencies}
    assert "fundamentals" in dep_kinds


@pytest.mark.asyncio
async def test_profitable_company_missing_when_loader_returns_none(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _none_loader(session, *, market, spec, limit, now):
        return None

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _none_loader,
    )
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
        from app.services.invest_view_model.fundamentals_screener import (
            FundamentalsScreenResult,
        )

        return FundamentalsScreenResult(
            rows=[],
            valuation_partition_date=dt.date(2026, 6, 2),
            fundamentals_partition_date=None,
            fundamentals_collected_at=None,
            fundamentals_state="missing",
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _fake_loader,
    )
    result = await screener_service.build_screener_results(
        preset_id="stable_growth",
        market="kr",
        session=_MockSession(),
        screening_service=_StubScreening(),
        resolver=_MockResolver(),
    )
    assert captured["preset_id"] == "stable_growth"  # registry routed the right spec
    assert result.freshness.primary.source == "market_valuation_snapshots"
    assert "fundamentals" in {d.kind for d in result.freshness.dependencies}


@pytest.mark.asyncio
async def test_cheap_value_empty_fundamentals_surfaces_missing_dependency(monkeypatch):
    from app.services.invest_view_model.fundamentals_screener import (
        FundamentalsScreenResult,
    )

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    async def _empty_fundamentals_loader(session, *, market, spec, limit, now):
        # valuation partition exists, but no fundamentals rows backfilled (Path B).
        return FundamentalsScreenResult(
            rows=[],
            valuation_partition_date=dt.date(2026, 6, 2),
            fundamentals_partition_date=None,
            fundamentals_collected_at=None,
            fundamentals_state="missing",
        )

    monkeypatch.setattr(
        "app.services.invest_view_model.fundamentals_screener.load_fundamentals_preset_from_snapshots",
        _empty_fundamentals_loader,
    )
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
