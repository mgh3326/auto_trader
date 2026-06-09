from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.investor_flow import InvestorFlowItem
from app.services.invest_view_model.screener_service import (
    _hydrate_investor_flow_chips,
    _investor_flow_chip_for_item,
    build_screener_results,
)
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
    InvestorFlowSnapshotUpsert,
)


def _item(**overrides) -> InvestorFlowItem:
    defaults = {
        "symbol": "403550",
        "market": "kr",
        "dataState": "fresh",
        "snapshotDate": dt.date(2026, 5, 12),
        "doubleBuy": False,
        "doubleSell": False,
        "foreignConsecutiveBuyDays": None,
        "foreignConsecutiveSellDays": None,
        "institutionConsecutiveBuyDays": None,
        "institutionConsecutiveSellDays": None,
        "individualConsecutiveBuyDays": None,
        "individualConsecutiveSellDays": None,
    }
    defaults.update(overrides)
    return InvestorFlowItem(**defaults)


@pytest.mark.unit
def test_double_buy_takes_precedence():
    chip = _investor_flow_chip_for_item(
        _item(
            doubleBuy=True,
            foreignConsecutiveBuyDays=4,
            institutionConsecutiveBuyDays=2,
        )
    )
    assert chip is not None
    assert chip.tone == "double_buy"
    assert "쌍끌이 매수" in chip.label


@pytest.mark.unit
def test_foreign_consecutive_buy_only():
    chip = _investor_flow_chip_for_item(_item(foreignConsecutiveBuyDays=5))
    assert chip is not None
    assert chip.tone == "foreign_buy"
    assert chip.label == "외국인 5일 순매수"


@pytest.mark.unit
def test_no_chip_when_signals_below_threshold():
    chip = _investor_flow_chip_for_item(_item(foreignConsecutiveBuyDays=2))
    assert chip is None


@pytest.mark.unit
def test_missing_state_yields_no_chip():
    chip = _investor_flow_chip_for_item(_item(dataState="missing"))
    assert chip is None


@pytest.mark.unit
def test_stale_state_annotates_label():
    chip = _investor_flow_chip_for_item(
        _item(dataState="stale", foreignConsecutiveBuyDays=4)
    )
    assert chip is not None
    assert chip.dataState == "stale"
    # ROB-430 트랙 B: the compact chip flags staleness without a hardcoded "1일"
    # (the page-level warning carries the precise lag); it must not understate.
    assert "지연" in chip.label
    assert "1일 지연" not in chip.label


@pytest.mark.unit
def test_double_sell_chip():
    chip = _investor_flow_chip_for_item(
        _item(
            doubleSell=True,
            foreignConsecutiveSellDays=3,
            institutionConsecutiveSellDays=2,
        )
    )
    assert chip is not None
    assert chip.tone == "double_sell"
    assert "쌍끌이 매도" in chip.label


@pytest.mark.unit
def test_institution_consecutive_buy():
    chip = _investor_flow_chip_for_item(_item(institutionConsecutiveBuyDays=4))
    assert chip is not None
    assert chip.tone == "institution_buy"
    assert "기관 4일 순매수" in chip.label


@pytest.mark.unit
def test_institution_consecutive_sell():
    chip = _investor_flow_chip_for_item(_item(institutionConsecutiveSellDays=3))
    assert chip is not None
    assert chip.tone == "institution_sell"
    assert "기관 3일 순매도" in chip.label


@pytest.mark.asyncio
async def test_hydrate_skips_non_kr_market():
    rows = [{"market": "us", "symbol": "AAPL"}]
    chips = await _hydrate_investor_flow_chips(
        db=SimpleNamespace(), market="us", rows=rows
    )
    assert chips == {}


@pytest.mark.asyncio
async def test_hydrate_swallows_provider_failures(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._latest_investor_flow_items",
        boom,
    )

    chips = await _hydrate_investor_flow_chips(
        db=SimpleNamespace(),
        market="kr",
        rows=[{"market": "kr", "symbol": "403550"}],
    )
    assert chips == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hydrate_returns_chip_for_matching_snapshot(monkeypatch):
    fake_item = _item(foreignConsecutiveBuyDays=4)
    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._latest_investor_flow_items",
        AsyncMock(return_value={"403550": fake_item}),
    )

    chips = await _hydrate_investor_flow_chips(
        db=SimpleNamespace(),
        market="kr",
        rows=[{"market": "kr", "symbol": "403550"}],
    )
    assert "403550" in chips
    assert chips["403550"].tone == "foreign_buy"


class _StubResolver:
    def relation(self, market: str, symbol: str) -> str:
        return "neither"


class _StubScreeningService:
    async def list_screening(self, **kwargs):
        return {
            "results": [],
            "warnings": ["fallback should not be used when snapshot path succeeds"],
            "timestamp": "2026-05-13T06:30:00+00:00",
            "cache_hit": False,
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_investor_flow_momentum_preset_uses_snapshot_discovery(
    db_session, monkeypatch
):
    repo = InvestorFlowSnapshotsRepository(db_session)
    # Keep the far-future partition so this is the MAX date regardless of prior
    # DB state.  Monkeypatch today_trading_date to return the same date so
    # classify_investor_flow_partition sees "snapshot_date == today" → "fresh".
    # (ROB-277 removed the old hardcoded _screener_snapshot_state="fresh", so
    # the test must ensure classification agrees with the partition date.)
    latest_partition = dt.date(2099, 5, 13)

    def _fake_today(market: str, *, now: object = None) -> dt.date:
        return latest_partition

    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.freshness.today_trading_date",
        _fake_today,
    )
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="403550",
            snapshot_date=latest_partition,
            foreign_net=20859,
            institution_net=-12931,
            individual_net=125586,
            foreign_net_buy_rank=3,
            foreign_consecutive_buy_days=4,
            source="naver_finance",
        )
    )
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="005930",
            snapshot_date=latest_partition,
            foreign_net=1000,
            institution_net=2000,
            individual_net=-3000,
            institution_consecutive_buy_days=2,
            source="naver_finance",
        )
    )
    await db_session.commit()

    from app.services.invest_screener_snapshots import partition_health
    from app.services.invest_screener_snapshots.partition_health import HealthyPartition

    monkeypatch.setattr(
        partition_health,
        "resolve_healthy_partition",
        AsyncMock(
            return_value=HealthyPartition(
                partition_date=latest_partition,
                row_count=9999,
                coverage_ratio=1.0,
                is_fallback=False,
                healthy=True,
            )
        ),
    )

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_service._should_use_snapshot_first",
        lambda service: True,
    )

    result = await build_screener_results(
        preset_id="investor_flow_momentum",
        screening_service=_StubScreeningService(),
        resolver=_StubResolver(),
        market="kr",
        session=db_session,
    )

    assert result.presetId == "investor_flow_momentum"
    assert result.title == "수급 모멘텀"
    assert result.metricLabel == "외국인 순매수"
    assert result.freshness.dataState == "fresh"
    assert [row.symbol for row in result.results] == ["005930", "403550"]
    double_buy_row = result.results[0]
    assert double_buy_row.metricValueLabel == "+1,000주"
    assert double_buy_row.investorFlowChip is not None
    assert double_buy_row.investorFlowChip.tone == "double_buy"
    assert "쌍끌이 매수" in double_buy_row.investorFlowChip.label
    foreign_streak_row = result.results[1]
    assert foreign_streak_row.metricValueLabel == "+20,859주"
    assert foreign_streak_row.investorFlowChip is not None
    assert foreign_streak_row.investorFlowChip.tone == "foreign_buy"
    assert foreign_streak_row.investorFlowChip.label == "외국인 4일 순매수"
    assert result.warnings == []
