from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import invest_api
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_market_parity import (
    InvestMarketParityCard,
    InvestMarketParityResponse,
    InvestParitySource,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()

    async def _stub_market_parity(**kwargs):
        assert kwargs["market"] == "kr"
        assert kwargs["include_disabled"] is False
        assert kwargs["limit"] == 2
        as_of = datetime(2026, 5, 14, 0, 0, tzinfo=UTC)
        return InvestMarketParityResponse(
            market="kr",
            state="fresh",
            asOf=as_of,
            cards=[
                InvestMarketParityCard(
                    id="ewy-kospi-implied-parity",
                    type="index_implied_parity",
                    title="EWY implied KOSPI parity",
                    baseSymbol="KOSPI",
                    proxySymbol="EWY",
                    basePrice=100,
                    proxyPrice=10,
                    fxRate=11,
                    impliedValue=110,
                    premiumPct=10,
                    tone="premium",
                    formula="((proxyPrice * fxRate * divisor) / basePrice - 1) * 100",
                    dataState="fresh",
                    source=InvestParitySource(
                        source="fixture",
                        sourceOfTruth="test_fixture",
                        asOf=as_of,
                    ),
                )
            ],
            warnings=[],
            notes=["Read-only market parity dashboard; no broker/order/watch mutations."],
        )

    monkeypatch.setattr(invest_api, "build_market_parity", _stub_market_parity)
    return TestClient(app)


@pytest.mark.unit
def test_get_market_parity_returns_camel_case_payload(client: TestClient) -> None:
    response = client.get("/invest/api/market/parity?includeDisabled=false&limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["market"] == "kr"
    assert body["state"] == "fresh"
    assert body["cards"][0]["dataState"] == "fresh"
    assert body["cards"][0]["source"]["sourceOfTruth"] == "test_fixture"
    assert body["cards"][0]["premiumPct"] == 10
    assert "source_of_truth" not in body["cards"][0]["source"]
    assert all("order_id" not in card for card in body["cards"])


@pytest.mark.unit
def test_get_market_parity_dash_alias_returns_payload(client: TestClient) -> None:
    response = client.get("/invest/api/market-parity?includeDisabled=false&limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["cards"][0]["title"] == "EWY implied KOSPI parity"


@pytest.mark.unit
def test_get_market_parity_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(invest_api_router)

    async def _stub_market_parity(**kwargs):
        raise AssertionError("must not call service without auth")

    monkeypatch.setattr(invest_api, "build_market_parity", _stub_market_parity)
    response = TestClient(app).get("/invest/api/market/parity")

    assert response.status_code in {401, 403}


@pytest.mark.unit
def test_get_market_parity_enforces_limit_bounds(client: TestClient) -> None:
    response = client.get("/invest/api/market/parity?limit=99")

    assert response.status_code == 422
