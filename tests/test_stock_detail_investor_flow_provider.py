from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import StockDetailInvestorFlow
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
        user_id=1, market="kr", symbol="403550", db=SimpleNamespace(),
        providers=providers,
    )
    assert response.investorFlow is not None
    assert response.investorFlow.dataState == "fresh"
    assert response.investorFlow.foreignNet == 450123
    assert response.investorFlow.doubleBuy is True


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
        user_id=1, market="us", symbol="AAPL", db=SimpleNamespace(),
        providers=providers,
    )
    assert response.investorFlow is None
    assert calls == []


@pytest.mark.asyncio
async def test_investor_flow_provider_failure_warns_but_keeps_response():
    async def boom(market, symbol, db):
        raise RuntimeError("db unavailable")

    providers = StockDetailProviders(
        resolver=_resolve_kr,
        investor_flow=boom,
    )
    response = await build_stock_detail(
        user_id=1, market="kr", symbol="403550", db=SimpleNamespace(),
        providers=providers,
    )
    assert response.investorFlow is None
    assert "investor_flow_unavailable" in response.meta.warnings


@pytest.mark.asyncio
async def test_kr_detail_investor_flow_defaults_to_none_without_snapshots(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.services.invest_view_model.stock_detail_service._latest_investor_flow_items",
        AsyncMock(return_value={}),
    )
    response = await build_stock_detail(
        user_id=1, market="kr", symbol="403550", db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=_resolve_kr),
    )
    assert response.investorFlow is not None
    assert response.investorFlow.dataState == "missing"
    assert response.investorFlow.foreignNet is None
