from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.invest_crypto import CryptoPendingOrdersSummary
from app.schemas.invest_stock_detail import (
    CryptoRecentTrades,
    StockDetailHolding,
    StockDetailOrderbook,
    StockDetailQuote,
)
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
async def test_build_stock_detail_uses_extended_timeout_for_holding(monkeypatch):
    from app.services.invest_view_model import stock_detail_service as service

    timeouts: dict[str, float] = {}

    async def capturing_optional_block(name, coro, warnings, timeout=3.0):
        timeouts[name] = timeout
        return await coro

    async def none_provider(*args, **kwargs):
        return None

    async def holding_provider(user_id, market, symbol, db):
        return StockDetailHolding(
            totalQuantity=1,
            tradeableQuantity=1,
            sellableQuantity=1,
            pendingSellQuantity=0,
            referenceQuantity=0,
            averageCost=70000,
            costBasis=70000,
            valueNative=71000,
            valueKrw=71000,
            pnlKrw=1000,
            pnlRate=0.014,
            includedSources=["kis"],
            priceState="live",
        )

    monkeypatch.setattr(service, "_run_optional_block", capturing_optional_block)

    await service.build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(),
        providers=StockDetailProviders(
            resolver=_resolve_kr,
            quote=none_provider,
            screener=none_provider,
            valuation=none_provider,
            holding=holding_provider,
            decision_history=none_provider,
            orderbook=none_provider,
            naver_enrichment=none_provider,
            discussion_signal=none_provider,
            investor_flow=none_provider,
        ),
    )

    assert timeouts["quote"] == pytest.approx(3.0)
    assert timeouts["holding"] > timeouts["quote"]


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
    async def no_quote_provider(market, symbol, db):
        return None

    async def no_orderbook_provider(market, symbol, db):
        return None

    async def no_recent_trades_provider(market, symbol, db):
        return None

    response = await build_stock_detail(
        user_id=1,
        market="crypto",
        symbol="KRW-BTC",
        db=SimpleNamespace(),
        providers=StockDetailProviders(
            resolver=_resolve_crypto,
            quote=no_quote_provider,
            orderbook=no_orderbook_provider,
            recent_trades=no_recent_trades_provider,
        ),
    )

    assert response.market == "crypto"
    assert response.fxSensitivity is not None
    assert response.fxSensitivity.status == "not_applicable"
    assert response.naverEnrichment is None
    assert response.orderbookSupport.supported is False
    assert response.orderbookSupport.reason == "provider_unavailable"


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


def test_build_stock_detail_crypto_includes_read_only_detail_and_preorder_check():
    async def quote_provider(market, symbol, db):
        assert market == "crypto"
        assert symbol == "KRW-BTC"
        return StockDetailQuote(
            price=100_000_000,
            previousClose=99_000_000,
            changeAmount=1_000_000,
            changeRate=1.01,
            asOf=datetime.now(UTC),
            priceState="live",
        )

    async def orderbook_provider(market, symbol, db):
        return StockDetailOrderbook(
            asks=[{"price": 100_100_000, "quantity": 0.5}],
            bids=[{"price": 100_000_000, "quantity": 0.4}],
        )

    async def recent_trades_provider(market, symbol, db):
        return CryptoRecentTrades(
            state="supported",
            items=[
                {
                    "priceKrw": 100_000_000,
                    "volume": 0.01,
                    "tradeTime": datetime.now(UTC),
                }
            ],
        )

    async def pending_orders_provider(user_id, market, symbol, db):
        return CryptoPendingOrdersSummary(items=[], emptyState="no_pending_orders")

    providers = StockDetailProviders(
        resolver=_resolve_crypto,
        quote=quote_provider,
        orderbook=orderbook_provider,
        recent_trades=recent_trades_provider,
        pending_orders=pending_orders_provider,
    )

    response = asyncio.run(
        build_stock_detail(
            user_id=7,
            market="crypto",
            symbol="btc",
            db=SimpleNamespace(),
            providers=providers,
        )
    )

    assert response.symbol == "KRW-BTC"
    assert response.orderbookSupport.supported is True
    assert response.orderbookSupport.reason is None
    assert response.capabilities.orderbook.supported is True
    assert response.capabilities.execution.supported is False
    assert response.capabilities.execution.reason == "read_only_mvp"
    assert response.cryptoDetail is not None
    assert response.cryptoDetail.profile.baseSymbol == "BTC"
    assert response.cryptoDetail.pendingOrders.emptyState == "no_pending_orders"
    assert response.cryptoDetail.preOrderChecklist.mode == "informational_only"
    assert any(
        item.key == "read_only_guardrail"
        for item in response.cryptoDetail.preOrderChecklist.items
    )


def test_build_stock_detail_crypto_orderbook_provider_unavailable_is_explicit():
    async def failing_orderbook_provider(market, symbol, db):
        raise RuntimeError("provider down")

    async def no_quote_provider(market, symbol, db):
        return None

    async def no_recent_trades_provider(market, symbol, db):
        return None

    providers = StockDetailProviders(
        resolver=_resolve_crypto,
        quote=no_quote_provider,
        orderbook=failing_orderbook_provider,
        recent_trades=no_recent_trades_provider,
    )

    response = asyncio.run(
        build_stock_detail(
            user_id=7,
            market="crypto",
            symbol="KRW-BTC",
            db=SimpleNamespace(),
            providers=providers,
        )
    )

    assert response.orderbook is None
    assert response.orderbookSupport.supported is False
    assert response.orderbookSupport.reason == "provider_unavailable"
    assert response.capabilities.orderbook.supported is False
    assert response.capabilities.orderbook.reason == "provider_unavailable"
    assert "orderbook_unavailable" in response.meta.warnings
    assert response.cryptoDetail is not None
    assert response.cryptoDetail.recentTrades.state == "unavailable"


@pytest.mark.asyncio
async def test_default_stock_detail_providers_are_not_noop_for_core_blocks():
    from app.services.invest_view_model.stock_detail_providers import (
        stock_detail_decision_history_provider,
        stock_detail_orderbook_provider,
        stock_detail_quote_provider,
        stock_detail_valuation_provider,
    )
    from app.services.invest_view_model.stock_detail_service import (
        DEFAULT_STOCK_DETAIL_PROVIDERS,
    )

    assert DEFAULT_STOCK_DETAIL_PROVIDERS.quote is stock_detail_quote_provider
    assert DEFAULT_STOCK_DETAIL_PROVIDERS.valuation is stock_detail_valuation_provider
    assert (
        DEFAULT_STOCK_DETAIL_PROVIDERS.decision_history
        is stock_detail_decision_history_provider
    )
    assert DEFAULT_STOCK_DETAIL_PROVIDERS.orderbook is stock_detail_orderbook_provider

@pytest.mark.asyncio
async def test_build_stock_detail_wires_decision_history():
    from app.schemas.invest_stock_detail import StockDetailDecisionHistory

    async def decision_history(market, symbol, db):
        assert symbol == "005930"
        return StockDetailDecisionHistory(symbol="005930", market="kr")

    providers = StockDetailProviders(
        resolver=_resolve_kr, decision_history=decision_history
    )

    result = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(execute=object()),
        providers=providers,
    )

    assert result.decisionHistory is not None
    assert result.decisionHistory.symbol == "005930"
    assert "decision_history_unavailable" not in result.meta.warnings


@pytest.mark.asyncio
async def test_build_stock_detail_isolates_decision_history_failure():
    async def decision_history(market, symbol, db):
        raise RuntimeError("boom")

    providers = StockDetailProviders(
        resolver=_resolve_kr, decision_history=decision_history
    )

    result = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(execute=object()),
        providers=providers,
    )

    assert result.decisionHistory is None
    assert "decision_history_unavailable" in result.meta.warnings
