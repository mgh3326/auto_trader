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
def test_screener_results_endpoint_normalizes_code_only_row() -> None:
    stub = _StubScreening(
        payload={
            "results": [
                {
                    "code": "005930",
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
    )
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=consecutive_gainers")
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["symbol"] == "005930"
    assert body["results"][0]["marketCapLabel"] == "478.0조원"


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


@pytest.mark.unit
def test_screener_results_endpoint_forwards_market_query() -> None:
    stub = _StubScreening(
        payload={
            "results": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "market": "us",
                    "sector": "Technology",
                    "market_cap_usd": 3_200_000_000_000,
                    "current_price": 210.4,
                    "change_rate": 1.5,
                    "change_amount": 3.1,
                    "volume": 50_000_000,
                    "per": 32.1,
                }
            ],
            "warnings": [],
        }
    )
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=cheap_value&market=us")

    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["market"] == "us"
    assert body["results"][0]["marketCapLabel"] == "$3.20T"
    assert stub.calls and stub.calls[0]["market"] == "us"


@pytest.mark.unit
def test_screener_consecutive_gainers_returns_streak_and_freshness() -> None:
    stub_payload = {
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
                "consecutive_up_days": 6,
                "week_change_rate": 8.50,
                "volume": 12_345_678,
            },
            {
                "symbol": "035720",
                "name": "카카오",
                "market": "kr",
                "sector": "인터넷",
                "market_cap_krw": 20_000_000_000_000,
                "close": 45_000,
                "change_rate": 0.8,
                "change_amount": 360,
                "consecutive_up_days": 5,
                "week_change_rate": 3.20,
                "volume": 3_000_000,
            },
        ],
        "warnings": [],
        "timestamp": "2026-05-10T05:30:00+00:00",
        "cache_hit": False,
    }
    stub = _StubScreening(payload=stub_payload)
    client = TestClient(_build_app(stub_screening=stub))

    r = client.get("/invest/api/screener/results?preset=consecutive_gainers&market=kr")

    assert r.status_code == 200
    body = r.json()
    assert body["presetId"] == "consecutive_gainers"
    # Freshness block must be present and correctly shaped
    freshness = body.get("freshness")
    assert freshness is not None
    assert freshness["asOfLabel"].endswith("기준")
    assert freshness["source"] in ("live", "cached", "previous_session")
    # Metric is now 1-week change rate (Toss-parity primary metric)
    results = body["results"]
    assert len(results) == 2
    for row in results:
        label = row["metricValueLabel"]
        assert "%" in label, f"Expected week_change_rate % label, got: {label}"
    # Verify the Toss-parity preset filters passed to the service
    assert stub.calls
    assert stub.calls[0].get("min_consecutive_up_days") == 5
    assert stub.calls[0].get("min_week_change_rate") == 0.0
    assert stub.calls[0].get("sort_by") == "week_change_rate"
    assert stub.calls[0].get("limit") == 80
