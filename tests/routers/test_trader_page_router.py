"""Task 889 — /trading/api/trader routes: GET-only, auth, stubbed service."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.trader_page import (
    TOSS_APP_ORDERS_NOTE,
    TOSS_FILL_POLLER_NOTE,
    TraderFillsResponse,
    TraderOpenOrdersCacheMeta,
    TraderOpenOrderSource,
    TraderOpenOrdersResponse,
    TraderWatchesResponse,
)

T0 = dt.datetime(2026, 9, 28, 3, 0, tzinfo=dt.UTC)


class _StubTraderPageService:
    def __init__(self) -> None:
        self.refresh_calls: list[bool] = []

    async def open_orders(self, *, refresh: bool = False) -> TraderOpenOrdersResponse:
        self.refresh_calls.append(refresh)
        return TraderOpenOrdersResponse(
            as_of=T0,
            data_state="degraded",
            count=0,
            items=[],
            sources=[
                TraderOpenOrderSource(
                    broker="toss",
                    market="kr",
                    status="unavailable",
                    count=0,
                    message="RuntimeError",
                    fetched_at=T0,
                    last_ok_at=dt.datetime(2026, 9, 28, 2, 30, tzinfo=dt.UTC),
                ),
                TraderOpenOrderSource(
                    broker="kis",
                    market="kr",
                    status="ok",
                    count=0,
                    fetched_at=T0,
                    last_ok_at=T0,
                ),
            ],
            cache=TraderOpenOrdersCacheMeta(ttl_seconds=45, cached_at=T0, hit=False),
            warnings=["toss/kr: RuntimeError"],
            notes=[TOSS_APP_ORDERS_NOTE],
            empty_reason=None,
        )

    async def fills_today(self) -> TraderFillsResponse:
        return TraderFillsResponse(
            day_kst="2026-09-28",
            window_start=dt.datetime(2026, 9, 28, 0, 0, tzinfo=dt.UTC),
            window_end=dt.datetime(2026, 9, 29, 0, 0, tzinfo=dt.UTC),
            count=0,
            items=[],
            data_state="missing",
            empty_reason="no fills in the requested window",
            notes=[TOSS_FILL_POLLER_NOTE],
        )

    async def active_watches(self) -> TraderWatchesResponse:
        return TraderWatchesResponse(as_of=T0, count=0, items=[])


def _make_client(service: _StubTraderPageService) -> TestClient:
    from app.routers import trader_page
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trader_page.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[trader_page.get_trader_page_service] = lambda: service
    return TestClient(app)


@pytest.mark.unit
def test_trader_routes_are_get_only() -> None:
    from app.routers import trader_page, trader_spa

    for module in (trader_page, trader_spa):
        for route in module.router.routes:
            methods = getattr(route, "methods", set()) or set()
            assert methods <= {"GET", "HEAD"}, (
                f"{module.__name__} route {getattr(route, 'path', '?')} "
                f"exposes {methods}"
            )


@pytest.mark.unit
def test_open_orders_endpoint_returns_per_broker_state() -> None:
    service = _StubTraderPageService()
    client = _make_client(service)

    response = client.get("/trading/api/trader/open-orders")

    assert response.status_code == 200
    body = response.json()
    assert body["data_state"] == "degraded"
    toss = next(s for s in body["sources"] if s["broker"] == "toss")
    assert toss["status"] == "unavailable"
    assert toss["last_ok_at"] == "2026-09-28T02:30:00Z"
    assert TOSS_APP_ORDERS_NOTE in body["notes"]
    assert body["cache"]["ttl_seconds"] == 45
    assert service.refresh_calls == [False]


@pytest.mark.unit
def test_open_orders_refresh_param_is_forwarded() -> None:
    service = _StubTraderPageService()
    client = _make_client(service)

    response = client.get("/trading/api/trader/open-orders?refresh=1")

    assert response.status_code == 200
    assert service.refresh_calls == [True]


@pytest.mark.unit
def test_fills_today_returns_kst_day_and_toss_note() -> None:
    service = _StubTraderPageService()
    client = _make_client(service)

    response = client.get("/trading/api/trader/fills/today")

    assert response.status_code == 200
    body = response.json()
    assert body["day_kst"] == "2026-09-28"
    assert body["count"] == 0
    assert TOSS_FILL_POLLER_NOTE in body["notes"]


@pytest.mark.unit
def test_watches_endpoint_returns_active_rows() -> None:
    service = _StubTraderPageService()
    client = _make_client(service)

    response = client.get("/trading/api/trader/watches")

    assert response.status_code == 200
    assert response.json()["count"] == 0


@pytest.mark.unit
def test_trader_api_requires_auth() -> None:
    """No session cookie -> dependency 401s before the service is touched."""
    from app.core.db import get_db
    from app.routers import trader_page

    service = _StubTraderPageService()
    app = FastAPI()
    app.include_router(trader_page.router)
    app.dependency_overrides[trader_page.get_trader_page_service] = lambda: service

    async def _fake_db():
        yield None

    app.dependency_overrides[get_db] = _fake_db

    client = TestClient(app, raise_server_exceptions=False)
    for path in (
        "/trading/api/trader/open-orders",
        "/trading/api/trader/fills/today",
        "/trading/api/trader/watches",
    ):
        response = client.get(path)
        assert response.status_code == 401, path
    assert service.refresh_calls == []


@pytest.mark.unit
def test_trader_api_middleware_401_and_spa_redirect() -> None:
    """AuthMiddleware: API paths get JSON 401, the page GET gets a 303 login."""
    from app.middleware.auth import AuthMiddleware

    inner = FastAPI()

    @inner.get("/trading/api/trader/open-orders")
    async def _orders() -> dict:  # pragma: no cover - unreachable when 401
        return {}

    client = TestClient(AuthMiddleware(inner), raise_server_exceptions=False)

    for path in (
        "/trading/api/trader/open-orders",
        "/trading/api/trader/open-orders?refresh=1",
        "/trading/api/trader/fills/today",
        "/trading/api/trader/watches",
    ):
        api = client.get(path)
        assert api.status_code == 401, path

    # Non-GET verbs carry no read or write authority either.
    for method in ("post", "put", "delete"):
        response = client.request(method, "/trading/api/trader/open-orders")
        assert response.status_code == 401, method
        page = client.request(method, "/trader/", follow_redirects=False)
        # Middleware passes non-GET HTML requests through to routing, which
        # has no non-GET handler — 404/405 prove nothing executed.
        assert page.status_code in (303, 404, 405), (method, page.status_code)

    page = client.get("/trader/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/web-auth/login")
