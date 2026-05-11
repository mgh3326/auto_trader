from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import StockDetailHolding, StockDetailOrderbook
from app.services.invest_view_model.stock_detail_service import build_stock_detail
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
        resolver=_resolve_us,
    )

    assert response.symbol == "BRK.B"
    assert response.market == "us"
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

    response = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(),
        resolver=_resolve_kr,
        holding_provider=holding_provider,
        orderbook_provider=orderbook_provider,
    )

    assert response.holding is not None
    assert response.holding.totalQuantity == 2
    assert response.naverEnrichment is not None
    assert response.naverEnrichment.naverCode == "005930"
    assert "discussionSignal.volume" in response.naverEnrichment.endpoints[-1].mappedFields
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
        resolver=_resolve_crypto,
    )

    assert response.market == "crypto"
    assert response.naverEnrichment is None
    assert response.orderbookSupport.supported is False
