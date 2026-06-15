from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.open_orders import OpenOrdersResponse


class _StubCurrentOrdersService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_open_orders(self, *, market: str = "all") -> OpenOrdersResponse:
        self.calls.append(market)
        return OpenOrdersResponse(
            market=market,  # type: ignore[arg-type]
            count=0,
            data_state="ok",
            as_of=dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
            items=[],
            sources=[],
            warnings=[],
            empty_reason="no open orders for the selected market",
        )


def _make_client(service: _StubCurrentOrdersService) -> TestClient:
    from app.routers import invest_open_orders
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_open_orders.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[invest_open_orders.get_current_orders_service] = lambda: (
        service
    )
    return TestClient(app)


@pytest.mark.unit
def test_open_orders_endpoint_defaults_to_all() -> None:
    service = _StubCurrentOrdersService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/open-orders")

    assert response.status_code == 200
    assert response.json()["market"] == "all"
    assert service.calls == ["all"]


@pytest.mark.unit
def test_open_orders_endpoint_accepts_market_filter() -> None:
    service = _StubCurrentOrdersService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/open-orders?market=crypto")

    assert response.status_code == 200
    assert response.json()["market"] == "crypto"
    assert service.calls == ["crypto"]


@pytest.mark.unit
def test_open_orders_endpoint_rejects_unknown_market() -> None:
    service = _StubCurrentOrdersService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/open-orders?market=paper")

    assert response.status_code == 422
    assert service.calls == []
