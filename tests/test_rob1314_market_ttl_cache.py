"""ROB-1314 — /market must serve external provider results from a TTL snapshot.

`build_market_dashboard()` (the default-provider production path used by the
route) re-hit Naver/yfinance/alternative.me providers on every request. The
fix memoizes the composed response behind a short process-local TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import app.routers.invest_api as invest_api
from app.routers.dependencies import get_authenticated_user
from app.services.invest_view_model import market_dashboard_service


@pytest.fixture(autouse=True)
def _fresh_ttl_cache():
    market_dashboard_service.reset_market_dashboard_cache()
    yield
    market_dashboard_service.reset_market_dashboard_cache()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_reuses_ttl_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"indices": 0, "fear_greed": 0, "kimchi": 0}

    def _patch(provider_cls: type, name: str, key: str) -> None:
        async def _method(self: Any) -> dict[str, Any]:
            calls[key] += 1
            return {}

        monkeypatch.setattr(provider_cls, name, _method)

    provider = market_dashboard_service.DefaultMarketDashboardProvider
    _patch(provider, "get_indices", "indices")
    _patch(provider, "get_fear_greed", "fear_greed")
    _patch(provider, "get_kimchi_premium", "kimchi")

    first = await market_dashboard_service.build_market_dashboard()
    second = await market_dashboard_service.build_market_dashboard()

    assert first.sections, "first build should compose sections"
    assert second.asOf == first.asOf, "TTL snapshot must reuse the composed response"
    assert calls == {"indices": 1, "fear_greed": 1, "kimchi": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_route_serves_snapshot_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    calls: dict[str, int] = {"indices": 0}

    async def _indices(self: Any) -> dict[str, Any]:
        calls["indices"] += 1
        return {
            "indices": [
                {
                    "name": "KOSPI",
                    "symbol": "KOSPI",
                    "current": 3000,
                    "change_pct": 0.5,
                    "data_state": "fresh",
                }
            ]
        }

    async def _fear_greed(self: Any) -> dict[str, Any]:
        return {"data": [{"value": 40, "value_classification": "Fear"}]}

    async def _kimchi(self: Any) -> list[dict[str, Any]]:
        return [{"premium_pct": 1.5, "symbol": "BTC"}]

    provider = market_dashboard_service.DefaultMarketDashboardProvider
    monkeypatch.setattr(provider, "get_indices", _indices)
    monkeypatch.setattr(provider, "get_fear_greed", _fear_greed)
    monkeypatch.setattr(provider, "get_kimchi_premium", _kimchi)

    app = FastAPI()
    app.include_router(invest_api.router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get("/invest/api/market")
        r2 = await client.get("/invest/api/market")

    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["asOf"] == r1.json()["asOf"]
    assert r1.json()["sections"][0]["metrics"][0]["value"] == "3,000.00"
    assert calls["indices"] == 1, (
        f"/market hit the external provider {calls['indices']} times within TTL"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_injected_provider_bypasses_ttl_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit provider injection (tests/tools) always builds fresh."""

    class _Provider:
        async def get_indices(self) -> dict[str, Any]:
            return {}

        async def get_fear_greed(self) -> dict[str, Any]:
            return {}

        async def get_kimchi_premium(self) -> list[dict[str, Any]]:
            return []

    first = await market_dashboard_service.build_market_dashboard(provider=_Provider())
    second = await market_dashboard_service.build_market_dashboard(provider=_Provider())
    assert first.state in {"missing", "error", "partial", "fresh"}
    assert isinstance(second, object)
    assert datetime.now(UTC) >= first.asOf
