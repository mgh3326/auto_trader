"""ROB-1314 — /stock-detail holding must read only the target symbol.

The badge/holding block used to run `build_account_panel_view` (the whole
account projection). It must instead project the single requested symbol from
the shared portfolio snapshot (ROB-1310 read model).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.invest_api as invest_api
from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import get_invest_home_service
from app.routers.invest_api import router as invest_api_router


class _SnapshotHoldingService:
    """Service seam ROB-1314 introduces: per-symbol snapshot projection."""

    def __init__(self) -> None:
        self.symbol_calls: list[dict[str, Any]] = []
        self.get_home_calls = 0
        self.panel_calls = 0

    async def get_home(self, **_kwargs: Any) -> Any:
        self.get_home_calls += 1
        raise AssertionError("stock detail called service.get_home()")

    async def build_account_panel_view(self, **_kwargs: Any) -> Any:
        self.panel_calls += 1
        raise AssertionError("stock detail called build_account_panel_view()")

    async def get_symbol_holding(
        self, *, user_id: int, market: str, symbol: str, **_kwargs: Any
    ) -> dict[str, Any] | None:
        self.symbol_calls.append(
            {"user_id": user_id, "market": market, "symbol": symbol}
        )
        if market == "kr" and symbol.upper() == "005930":
            return {
                "totalQuantity": 10.0,
                "tradeableQuantity": 8.0,
                "sellableQuantity": 8.0,
                "pendingSellQuantity": 2.0,
                "referenceQuantity": 0.0,
                "averageCost": 70000.0,
                "costBasis": 700000.0,
                "valueNative": 720000.0,
                "valueKrw": 720000.0,
                "pnlKrw": 20000.0,
                "pnlRate": 20000 / 700000,
                "includedSources": ["kis"],
                "priceState": "live",
            }
        return None


def _build_app(service: _SnapshotHoldingService) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 7}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: service

    async def _db_dep():
        yield object()

    app.dependency_overrides[get_db] = _db_dep

    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stock_detail_holding_reads_only_target_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route must not fan out to the whole account panel for one symbol."""
    from app.schemas.invest_stock_detail import StockDetailResponse

    service = _SnapshotHoldingService()

    async def _build_stock_detail(**kwargs: Any) -> StockDetailResponse:
        holding = await kwargs["providers"].holding(
            kwargs["user_id"], kwargs["market"], "005930", kwargs["db"]
        )
        return StockDetailResponse(
            symbol="005930",
            market=kwargs["market"],
            displayName="삼성전자",
            exchange="KRX",
            instrumentType="equity",
            currency="KRW",
            assetType="equity",
            assetCategory="kr_stock",
            holding=holding,
            orderbookSupport={"supported": False, "reason": "kr_unavailable"},
            capabilities={},
            meta={"computedAt": datetime.now(UTC), "warnings": []},
        )

    monkeypatch.setattr(invest_api, "build_stock_detail", _build_stock_detail)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.get("/invest/api/stock-detail/kr/005930")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["holding"]["totalQuantity"] == 10.0
    assert body["holding"]["averageCost"] == 70000.0
    assert service.symbol_calls == [{"user_id": 7, "market": "kr", "symbol": "005930"}]
    assert service.get_home_calls == 0
    assert service.panel_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_symbol_holding_projects_from_shared_snapshot_without_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm shared snapshot must answer the per-symbol lookup with zero reader fetches."""
    from app.services import invest_home_service as svc_mod
    from app.services.invest_home_readers import _SourceFetchResult
    from app.services.invest_home_service import InvestHomeService
    from app.services.portfolio_snapshot import portfolio_snapshot_scope
    from app.services.portfolio_snapshot_cache import PortfolioSnapshotCache

    calls = {"reader": 0}

    class _Reader:
        source = "manual"

        async def fetch(self, *, user_id: int) -> _SourceFetchResult:
            calls["reader"] += 1
            return _SourceFetchResult(accounts=[], holdings=[])

        async def fetch_held_pairs(self, *, user_id: int):
            return []

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = PortfolioSnapshotCache(redis_client=redis_client, ttl_seconds=30)

    payload = {
        "schema_version": 1,
        "held_pairs": [["kr", "005930"]],
        "response": {
            "homeSummary": {
                "includedSources": ["kis"],
                "excludedSources": [],
                "totalValueKrw": 720000,
            },
            "accounts": [],
            "holdings": [],
            "groupedHoldings": [
                {
                    "groupId": "KR:equity:KRW:005930",
                    "symbol": "005930",
                    "market": "KR",
                    "assetType": "equity",
                    "assetCategory": "kr_stock",
                    "displayName": "삼성전자",
                    "currency": "KRW",
                    "totalQuantity": 10.0,
                    "tradeableQuantity": 8.0,
                    "referenceQuantity": 0.0,
                    "averageCost": 70000.0,
                    "costBasis": 700000.0,
                    "valueNative": 720000.0,
                    "valueKrw": 720000.0,
                    "pnlKrw": 20000.0,
                    "pnlRate": 20000 / 700000,
                    "includedSources": ["kis"],
                    "sourceBreakdown": [],
                }
            ],
            "meta": {
                "warnings": [],
                "hiddenCounts": {"upbitInactive": 0, "upbitDust": 0},
                "hiddenHoldings": [],
            },
        },
    }
    scope = portfolio_snapshot_scope(user_id=7, include_paper=False, paper_sources=None)
    await cache.put(scope, payload)

    monkeypatch.setattr(svc_mod, "_fetch_reader_result", None)  # readers unusable

    service = InvestHomeService(
        kis_reader=None,
        upbit_reader=None,
        manual_reader=_Reader(),
        snapshot_cache=cache,
    )
    monkeypatch.setattr(service, "_get_home_uncached", None)

    holding = await service.get_symbol_holding(user_id=7, market="kr", symbol="005930")
    assert holding is not None
    assert holding.totalQuantity == 10.0
    assert holding.averageCost == 70000.0
    assert calls["reader"] == 0

    missing = await service.get_symbol_holding(user_id=7, market="us", symbol="AAPL")
    assert missing is None
