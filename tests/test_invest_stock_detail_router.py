from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stock_detail_route_passes_snapshot_symbol_holding_provider(monkeypatch):
    from app.routers import invest_api
    from app.schemas.invest_stock_detail import StockDetailResponse

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
        def __init__(self):
            self.calls: list[dict] = []

        async def get_symbol_holding(
            self, *, user_id, market, symbol, include_paper=False, paper_sources=None
        ):
            self.calls.append(
                {
                    "user_id": user_id,
                    "market": market,
                    "symbol": symbol,
                    "include_paper": include_paper,
                }
            )
            return SimpleNamespace(
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

    service = FakeHomeService()
    monkeypatch.setattr(invest_api, "build_stock_detail", fake_build_stock_detail)

    response = await invest_api.get_stock_detail(
        market="kr",
        symbol="000270",
        user=SimpleNamespace(id=7),
        db=SimpleNamespace(),
        service=service,
    )

    assert response.holding is not None
    assert response.holding.totalQuantity == 4
    assert response.holding.includedSources == ["kis"]
    assert service.calls == [
        {
            "user_id": 7,
            "market": "kr",
            "symbol": "000270",
            "include_paper": False,
        }
    ]
