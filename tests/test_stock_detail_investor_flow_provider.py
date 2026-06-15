from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import (
    StockDetailInvestorFlow,
    StockDetailInvestorFlowDailyRow,
)
from app.services.invest_view_model.stock_detail_service import (
    StockDetailProviders,
    build_stock_detail,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import ResolvedSymbol


async def _resolve_kr(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db=raw_symbol,
        display_name="에스케이엔펄스",
        exchange="KOSPI",
        instrument_type="equity_kr",
        asset_type="equity",
        asset_category="kr_stock",
        currency="KRW",
    )


async def _resolve_us(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db=raw_symbol,
        display_name="Apple Inc",
        exchange="NASDAQ",
        instrument_type="equity_us",
        asset_type="equity",
        asset_category="us_stock",
        currency="USD",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_detail_includes_investor_flow_when_provider_returns_payload():
    async def fake_investor_flow(market, symbol, db):
        return StockDetailInvestorFlow(
            symbol=symbol,
            dataState="fresh",
            snapshotDate="2026-05-12",
            snapshotSource="naver_finance",
            foreignNet=450123,
            institutionNet=120044,
            individualNet=-570167,
            foreignConsecutiveBuyDays=3,
            doubleBuy=True,
        )

    providers = StockDetailProviders(
        resolver=_resolve_kr,
        investor_flow=fake_investor_flow,
    )
    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="403550",
        db=SimpleNamespace(),
        providers=providers,
    )
    assert response.investorFlow is not None
    assert response.investorFlow.dataState == "fresh"
    assert response.investorFlow.foreignNet == 450123
    assert response.investorFlow.doubleBuy is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_detail_does_not_call_investor_flow_provider():
    calls = []

    async def fake_investor_flow(market, symbol, db):
        calls.append((market, symbol))
        return None

    providers = StockDetailProviders(
        resolver=_resolve_us,
        investor_flow=fake_investor_flow,
    )
    response = await build_stock_detail(
        user_id=1,
        market="us",
        symbol="AAPL",
        db=SimpleNamespace(),
        providers=providers,
    )
    assert response.investorFlow is None
    assert calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_investor_flow_provider_failure_warns_but_keeps_response():
    async def boom(market, symbol, db):
        raise RuntimeError("db unavailable")

    providers = StockDetailProviders(
        resolver=_resolve_kr,
        investor_flow=boom,
    )
    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="403550",
        db=SimpleNamespace(),
        providers=providers,
    )
    assert response.investorFlow is None
    assert "investor_flow_unavailable" in response.meta.warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_detail_investor_flow_defaults_to_none_without_snapshots(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.services.invest_view_model.stock_detail_service._latest_investor_flow_items",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.stock_detail_service._recent_investor_flow_rows",
        AsyncMock(return_value=[]),
    )
    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="403550",
        db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=_resolve_kr),
    )
    assert response.investorFlow is not None
    assert response.investorFlow.dataState == "missing"
    assert response.investorFlow.foreignNet is None
    assert response.investorFlow.dailyRows == []
    assert response.investorFlow.periodSummary is None
    assert response.investorFlow.buyerDecomposition is None
    assert response.investorFlow.unavailableLabels == []


def test_daily_row_from_snapshot_carries_persisted_market_fields():
    from app.services.invest_view_model.stock_detail_service import (
        _daily_row_from_snapshot,
    )

    row = SimpleNamespace(
        snapshot_date=date(2026, 5, 13),
        collected_at=None,
        source="naver_finance",
        close=Decimal("75000"),
        change_rate=Decimal("2.5"),
        volume=15_118_684,
        foreign_net=20_859,
        foreign_holding_shares=2_790_424_635,
        foreign_holding_rate=Decimal("47.73"),
        institution_net=-12_931,
        individual_net=125_586,
        double_buy=False,
        double_sell=False,
    )

    out = _daily_row_from_snapshot(row)

    assert out.close == 75000.0
    assert out.changeRate == 2.5
    assert out.volume == 15_118_684
    assert out.foreignHoldingShares == 2_790_424_635
    assert out.foreignHoldingRate == 47.73


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_detail_default_investor_flow_includes_daily_rows(monkeypatch):
    from datetime import date
    from unittest.mock import AsyncMock

    from app.schemas.investor_flow import InvestorFlowItem

    monkeypatch.setattr(
        "app.services.invest_view_model.stock_detail_service._latest_investor_flow_items",
        AsyncMock(
            return_value={
                "403550": InvestorFlowItem(
                    symbol="403550",
                    dataState="fresh",
                    snapshotDate=date(2026, 5, 13),
                    source="naver_finance",
                    foreignNet=20859,
                    institutionNet=-12931,
                    individualNet=125586,
                    foreignConsecutiveBuyDays=4,
                )
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.invest_view_model.stock_detail_service._recent_investor_flow_rows",
        AsyncMock(
            return_value=[
                StockDetailInvestorFlowDailyRow(
                    snapshotDate="2026-05-13",
                    source="naver_finance",
                    close=75000,
                    changeRate=2.5,
                    volume=15_118_684,
                    foreignNet=20859,
                    foreignHoldingShares=2_790_424_635,
                    foreignHoldingRate=47.73,
                    institutionNet=-12931,
                    individualNet=125586,
                ),
                StockDetailInvestorFlowDailyRow(
                    snapshotDate="2026-05-12",
                    source="naver_finance",
                    close=73500,
                    changeRate=-1.2,
                    volume=10_000_000,
                    foreignNet=440,
                    foreignHoldingShares=2_790_400_000,
                    foreignHoldingRate=47.71,
                    institutionNet=-1024,
                    individualNet=590,
                ),
            ]
        ),
    )

    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="403550",
        db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=_resolve_kr),
    )

    assert response.investorFlow is not None
    assert response.investorFlow.dailyRows[0].snapshotDate == "2026-05-13"
    assert response.investorFlow.dailyRows[0].foreignNet == 20859
    assert response.investorFlow.dailyRows[0].close == 75000
    assert response.investorFlow.dailyRows[0].changeRate == 2.5
    assert response.investorFlow.dailyRows[0].volume == 15_118_684
    assert response.investorFlow.dailyRows[0].foreignHoldingRate == 47.73
    assert response.investorFlow.periodSummary is not None
    assert response.investorFlow.periodSummary.windowDays == 2
    assert response.investorFlow.periodSummary.foreignNetTotal == 21299
    assert response.investorFlow.periodSummary.institutionNetTotal == -13955
    assert response.investorFlow.periodSummary.individualNetTotal == 126176
    assert response.investorFlow.periodSummary.foreignBuyDays == 2
    assert response.investorFlow.periodSummary.foreignNetToVolumeRatio == pytest.approx(
        21299 / 25_118_684
    )
    assert response.investorFlow.periodSummary.foreignHoldingSharesChange == 24_635
    assert response.investorFlow.periodSummary.foreignHoldingRateChange == pytest.approx(
        0.02
    )
    assert response.investorFlow.buyerDecomposition is not None
    assert response.investorFlow.buyerDecomposition.leadingBuyer == "individual"
    assert response.investorFlow.buyerDecomposition.label == "개인 주도"
    assert response.investorFlow.unavailableLabels == []
    assert response.investorFlow.periodSummary.unavailableLabels == []
    assert "지연된 과거 참고 데이터" in response.investorFlow.cautionLabel
