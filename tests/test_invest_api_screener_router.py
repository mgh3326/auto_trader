"""ROB-147 — router tests for /invest/api/screener/{presets,results}."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import (
    get_invest_home_service,
    get_screener_service_dep,
)
from app.routers.invest_api import (
    router as invest_api_router,
)
from app.schemas.invest_home import (
    InvestHomeResponse,
    InvestHomeResponseMeta,
)
from app.services.invest_home_service import build_grouped_holdings, build_home_summary


class _StubHomeService:
    async def get_home(self, *, user_id: int) -> InvestHomeResponse:
        return InvestHomeResponse(
            homeSummary=build_home_summary([]),
            accounts=[],
            holdings=[],
            groupedHoldings=build_grouped_holdings([]),
            meta=InvestHomeResponseMeta(warnings=[]),
        )


class _StubScreening:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._payload = payload or {
            "results": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "market": "kr",
                    "sector": "반도체",
                    "market_cap_krw": 478_000_000_000_000,
                    "close": 80_000,
                    "change_rate": 1.23,
                    "change_amount": 970,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
        self.list_screening = AsyncMock(side_effect=self._list)

    async def _list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._payload


def _build_app(stub_screening: _StubScreening | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: _StubHomeService()
    if stub_screening is not None:
        app.dependency_overrides[get_screener_service_dep] = lambda: stub_screening
    return app


@pytest.mark.unit
def test_screener_presets_endpoint_returns_catalog() -> None:
    client = TestClient(_build_app())
    r = client.get("/invest/api/screener/presets")
    assert r.status_code == 200
    body = r.json()
    assert len(body["presets"]) >= 6
    assert body["selectedPresetId"] == "consecutive_gainers"
    assert any(p["id"] == "consecutive_gainers" for p in body["presets"])


@pytest.mark.unit
def test_screener_results_endpoint_happy_path() -> None:
    stub = _StubScreening()
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=consecutive_gainers")
    assert r.status_code == 200
    body = r.json()
    assert body["presetId"] == "consecutive_gainers"
    assert body["title"] == "연속 상승세"
    assert len(body["results"]) == 1
    assert body["results"][0]["symbol"] == "005930"
    assert stub.calls and stub.calls[0]["market"] == "kr"


@pytest.mark.unit
def test_screener_results_endpoint_unknown_preset_returns_empty_with_warning() -> None:
    stub = _StubScreening()
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=__unknown__")
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    assert body["warnings"]
    assert stub.calls == []


@pytest.mark.unit
def test_screener_results_endpoint_requires_preset_param() -> None:
    stub = _StubScreening()
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results")
    assert r.status_code == 422  # missing required query param
