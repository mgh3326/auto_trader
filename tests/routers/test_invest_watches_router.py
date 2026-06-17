from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.invest_watches import WatchesResponse


class _StubWatchPanelService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def list_watches(
        self,
        *,
        market: str = "all",
        status: str = "all",
        symbol: str | None = None,
    ) -> WatchesResponse:
        self.calls.append((market, status, symbol))
        return WatchesResponse(
            market=market,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            count=0,
            data_state="ok",
            as_of=dt.datetime(2026, 6, 17, 0, 0, tzinfo=dt.UTC),
            items=[],
            warnings=[],
            empty_reason="No watch alerts found.",
        )


def _make_client(service: _StubWatchPanelService) -> TestClient:
    from app.routers import invest_watches
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_watches.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[invest_watches.get_watch_panel_service] = lambda: service
    return TestClient(app)


@pytest.mark.unit
def test_watches_endpoint_defaults_to_all() -> None:
    service = _StubWatchPanelService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/watches")

    assert response.status_code == 200
    assert response.json()["market"] == "all"
    assert response.json()["status"] == "all"
    assert service.calls == [("all", "all", None)]


@pytest.mark.unit
def test_watches_endpoint_accepts_filters() -> None:
    service = _StubWatchPanelService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/watches?market=crypto&status=active")

    assert response.status_code == 200
    assert response.json()["market"] == "crypto"
    assert response.json()["status"] == "active"
    assert service.calls == [("crypto", "active", None)]


@pytest.mark.unit
def test_watches_endpoint_forwards_symbol_filter() -> None:
    service = _StubWatchPanelService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/watches?market=kr&symbol=005930")

    assert response.status_code == 200
    assert service.calls == [("kr", "all", "005930")]


@pytest.mark.unit
def test_watches_endpoint_rejects_unknown_filters() -> None:
    service = _StubWatchPanelService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/watches?market=paper")
    assert response.status_code == 422

    response = client.get("/trading/api/invest/watches?status=done")
    assert response.status_code == 422


@pytest.mark.unit
def test_watches_default_service_receives_db_dependency(monkeypatch) -> None:
    from app.core.db import get_db
    from app.routers import invest_watches
    from app.routers.dependencies import get_authenticated_user

    captured = {}

    class _Service:
        def __init__(self, *, db):
            captured["db"] = db

        async def list_watches(
            self,
            *,
            market: str = "all",
            status: str = "all",
            symbol: str | None = None,
        ) -> WatchesResponse:
            return WatchesResponse(
                market=market,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                count=0,
                data_state="ok",
                as_of=dt.datetime(2026, 6, 17, 0, 0, tzinfo=dt.UTC),
                items=[],
                warnings=[],
                empty_reason="No watch alerts found.",
            )

    monkeypatch.setattr(invest_watches, "WatchPanelService", _Service)

    app = FastAPI()
    app.include_router(invest_watches.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db-session"

    response = TestClient(app).get("/trading/api/invest/watches")

    assert response.status_code == 200
    assert captured["db"] == "db-session"
