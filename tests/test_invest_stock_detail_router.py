from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stock_detail_route_passes_account_panel_holding_provider(monkeypatch):
    from app.routers import invest_api
    from app.schemas.invest_stock_detail import StockDetailResponse
    from app.services.invest_view_model.stock_detail_symbol_resolver import ResolvedSymbol

    async def fake_build_stock_detail(*, user_id, market, symbol, db, providers):
        holding = await providers.holding(user_id, market, symbol, db)
        return StockDetailResponse(
            symbol=symbol,
            market=market,
            displayName="기아",
            exchange="KOSPI",
            instrumentType="equity_kr",
            currency="KRW",
            assetType="equity",
            assetCategory="kr_stock",
            quote=None,
            holding=holding,
            orderbookSupport={"supported": False, "reason": "kr_unavailable"},
            orderbook=None,
            capabilities={},
            meta={"computedAt": "2026-06-15T00:00:00Z", "warnings": []},
        )

    class FakeHomeService:
        async def build_account_panel_view(self, *, user_id, include_paper=False, paper_sources=None):
            assert include_paper is False
            return SimpleNamespace(
                groupedHoldings=[
                    SimpleNamespace(
                        symbol="000270",
                        market="KR",
                        totalQuantity=4,
                        tradeableQuantity=4,
                        sellableQuantity=4,
                        pendingSellQuantity=0,
                        referenceQuantity=0,
                        averageCost=70000,
                        costBasis=280000,
                        valueNative=300000,
                        valueKrw=300000,
                        pnlKrw=20000,
                        pnlRate=0.0714,
                        includedSources=["kis"],
                        priceState="live",
                    )
                ]
            )

    monkeypatch.setattr(invest_api, "build_stock_detail", fake_build_stock_detail)

    response = await invest_api.get_stock_detail(
        market="kr",
        symbol="000270",
        user=SimpleNamespace(id=7),
        db=SimpleNamespace(),
        service=FakeHomeService(),
    )

    assert response.holding is not None
    assert response.holding.totalQuantity == 4
    assert response.holding.includedSources == ["kis"]
