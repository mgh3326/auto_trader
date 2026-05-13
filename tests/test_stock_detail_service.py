from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import StockDetailHolding, StockDetailOrderbook
from app.services.invest_view_model.stock_detail_service import (
    StockDetailProviders,
    build_stock_detail,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import ResolvedSymbol


async def _resolve_us(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db="BRK.B",
        display_name="버크셔해서웨이 B",
        exchange="NYSE",
        instrument_type="equity_us",
        asset_type="equity",
        asset_category="us_stock",
        currency="USD",
    )


async def _resolve_kr(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db="005930",
        display_name="삼성전자",
        exchange="KOSPI",
        instrument_type="equity_kr",
        asset_type="equity",
        asset_category="kr_stock",
        currency="KRW",
    )


async def _resolve_crypto(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db="KRW-BTC",
        display_name="비트코인",
        exchange="Upbit",
        instrument_type="crypto",
        asset_type="crypto",
        asset_category="crypto",
        currency="KRW",
    )


@pytest.mark.asyncio
async def test_build_stock_detail_declares_us_orderbook_unsupported():
    response = await build_stock_detail(
        user_id=1,
        market="us",
        symbol="BRK-B",
        db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=_resolve_us),
    )

    assert response.symbol == "BRK.B"
    assert response.market == "us"
    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "missing_holding"
    assert "매수" not in response.fxSensitivity.caution
    assert "매도" not in response.fxSensitivity.caution
    assert response.naverEnrichment is not None
    assert response.naverEnrichment.naverCode == "BRK.B"
    assert response.naverEnrichment.liveFetchEnabled is False
    assert response.naverEnrichment.endpoints[0].status == "verified_200"
    assert response.orderbook is None
    assert response.orderbookSupport.supported is False
    assert response.orderbookSupport.reason == "us_unsupported"
    assert response.capabilities.orderbook.supported is False
    assert response.capabilities.execution.supported is False
    assert response.capabilities.options.supported is False


@pytest.mark.asyncio
async def test_build_stock_detail_maps_holding_and_kr_orderbook_when_available():
    async def holding_provider(user_id, market, symbol, db):
        await asyncio.sleep(0)
        return StockDetailHolding(
            totalQuantity=2,
            averageCost=70000,
            costBasis=140000,
            valueNative=142000,
            valueKrw=142000,
            pnlKrw=2000,
            pnlRate=0.014,
            includedSources=["kis"],
            priceState="live",
        )

    async def orderbook_provider(market, symbol, db):
        return StockDetailOrderbook(
            asOf=datetime.now(UTC),
            asks=[{"price": 71200, "quantity": 10}],
            bids=[{"price": 71100, "quantity": 12}],
        )

    fx_rate_called = False

    async def fx_rate_provider():
        nonlocal fx_rate_called
        await asyncio.sleep(0)
        fx_rate_called = True
        return 1360.0

    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(),
        providers=StockDetailProviders(
            resolver=_resolve_kr,
            holding=holding_provider,
            orderbook=orderbook_provider,
            fx_rate=fx_rate_provider,
        ),
    )

    assert response.holding is not None
    assert response.holding.totalQuantity == 2
    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "not_applicable"
    assert response.fxSensitivity.scenarios == []
    assert fx_rate_called is False
    assert response.naverEnrichment is not None
    assert response.naverEnrichment.naverCode == "005930"
    assert (
        "discussionSignal.volume" in response.naverEnrichment.endpoints[-1].mappedFields
    )
    assert response.orderbookSupport.supported is True
    assert response.orderbook is not None
    assert response.orderbook.asks[0].price == 71200


@pytest.mark.asyncio
async def test_build_stock_detail_omits_naver_poc_for_crypto():
    response = await build_stock_detail(
        user_id=1,
        market="crypto",
        symbol="KRW-BTC",
        db=SimpleNamespace(),
        providers=StockDetailProviders(resolver=_resolve_crypto),
    )

    assert response.market == "crypto"
    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "not_applicable"
    assert response.naverEnrichment is None
    assert response.orderbookSupport.supported is False


@pytest.mark.asyncio
async def test_build_stock_detail_adds_fx_sensitivity_for_us_holding():
    async def holding_provider(user_id, market, symbol, db):
        await asyncio.sleep(0)
        return StockDetailHolding(
            totalQuantity=2,
            averageCost=200,
            costBasis=400,
            valueNative=422.68,
            valueKrw=575000,
            pnlKrw=30000,
            pnlRate=0.055,
            includedSources=["kis"],
            priceState="live",
        )

    async def fx_rate_provider():
        await asyncio.sleep(0)
        return 1360.0

    response = await build_stock_detail(
        user_id=1,
        market="us",
        symbol="BRK-B",
        db=SimpleNamespace(),
        providers=StockDetailProviders(
            resolver=_resolve_us,
            holding=holding_provider,
            fx_rate=fx_rate_provider,
        ),
    )

    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "available"
    assert response.fxSensitivity.baseFxRate == pytest.approx(1360.0)
    assert response.fxSensitivity.holdingValueNative == pytest.approx(422.68)
    assert [scenario.rateMovePct for scenario in response.fxSensitivity.scenarios] == [
        -1.0,
        1.0,
    ]
    assert response.fxSensitivity.scenarios[0].estimatedKrwImpact == pytest.approx(
        -5748.448
    )
    assert response.fxSensitivity.scenarios[1].estimatedKrwImpact == pytest.approx(
        5748.448
    )
    assert "매수" not in response.fxSensitivity.caution
    assert "매도" not in response.fxSensitivity.caution
    assert "추천" not in response.fxSensitivity.caution


@pytest.mark.asyncio
async def test_build_stock_detail_degrades_when_us_fx_provider_fails():
    async def holding_provider(user_id, market, symbol, db):
        await asyncio.sleep(0)
        return StockDetailHolding(
            totalQuantity=2,
            averageCost=200,
            costBasis=400,
            valueNative=422.68,
            valueKrw=575000,
            pnlKrw=30000,
            pnlRate=0.055,
            includedSources=["kis"],
            priceState="live",
        )

    async def fx_rate_provider():
        await asyncio.sleep(0)
        raise RuntimeError("provider unavailable")

    response = await build_stock_detail(
        user_id=1,
        market="us",
        symbol="BRK-B",
        db=SimpleNamespace(),
        providers=StockDetailProviders(
            resolver=_resolve_us,
            holding=holding_provider,
            fx_rate=fx_rate_provider,
        ),
    )

    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "missing_fx_rate"
    assert response.fxSensitivity.scenarios == []
    assert "fx_sensitivity_unavailable" in response.meta.warnings
