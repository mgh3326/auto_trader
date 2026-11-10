from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers import retro_v1

SPEC = Path("contracts/retro/v1/openapi.yaml")
EXAMPLES = Path("contracts/retro/v1/examples")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(settings, "RETRO_API_ENABLED", True)
    monkeypatch.setattr(settings, "RETRO_API_READ_TOKEN", "retro-secret")
    app = FastAPI()
    app.include_router(retro_v1.router)
    app.add_middleware(AuthMiddleware)
    app.dependency_overrides[get_db] = lambda: object()
    return app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def routes():
    return {
        (method.lower(), route.path)
        for route in retro_v1.router.routes
        for method in route.methods
    }


def test_routes_match_contract():
    spec = yaml.safe_load(SPEC.read_text())
    expected = {
        (method, path)
        for path, operations in spec["paths"].items()
        for method in operations
    }
    assert routes() == expected
    from app.main import app

    actual = {
        (method.lower(), route.path)
        for route in app.routes
        if hasattr(route, "methods") and route.path.startswith(retro_v1.PREFIX + "/")
        for method in route.methods
    }
    assert actual == expected


@pytest.mark.asyncio
async def test_gates_before_session(client, app, monkeypatch):
    def trap():
        raise AssertionError("session opened")

    app.dependency_overrides[get_db] = trap
    monkeypatch.setattr(settings, "RETRO_API_ENABLED", False)
    for op in retro_v1.OPERATIONS:
        assert (await client.get(f"{retro_v1.PREFIX}/{op}")).status_code == 404
    monkeypatch.setattr(settings, "RETRO_API_ENABLED", True)
    monkeypatch.setattr(settings, "RETRO_API_READ_TOKEN", "")
    assert (await client.get(f"{retro_v1.PREFIX}/data_gaps")).status_code == 403
    monkeypatch.setattr(settings, "RETRO_API_READ_TOKEN", "retro-secret")
    for headers in (
        {},
        {"X-Retro-Read-Token": "wrong"},
        {"X-Retro-Read-Token": "execution-token"},
        {"X-Retro-Read-Token": "hermes-token"},
        {"X-Retro-Read-Token": "mcp-token"},
        {"X-Retro-Read-Token": "access-assertion"},
        {"Cookie": "session=valid"},
        {"X-Execution-Ledger-Ingest-Token": "machine"},
        {"X-Hermes-Ingest-Token": "machine"},
        {"Authorization": "Bearer machine"},
        {"Cf-Access-Jwt-Assertion": "machine"},
    ):
        assert (
            await client.get(f"{retro_v1.PREFIX}/data_gaps", headers=headers)
        ).status_code == 401
    headers = {"X-Retro-Read-Token": "retro-secret"}
    assert (
        await client.get(f"{retro_v1.PREFIX}/unknown", headers=headers)
    ).status_code == 403
    assert (
        await client.post(f"{retro_v1.PREFIX}/data_gaps", headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_readers_return_contract_examples(client, monkeypatch):
    examples = [yaml.safe_load(p.read_text()) for p in EXAMPLES.glob("*.yaml")]
    for op in retro_v1.OPERATIONS:
        example = next(
            e for e in examples if e["operation"] == op and e["status"] == 200
        )
        mocks = {
            name: AsyncMock(return_value=example["response"])
            for name in retro_v1.OPERATIONS
        }
        for name, mock in mocks.items():
            monkeypatch.setitem(retro_v1.READERS, name, mock)
        response = await client.get(
            example["request"]["path"],
            params=example["request"]["query"],
            headers={"X-Retro-Read-Token": "retro-secret"},
        )
        assert response.status_code == 200, f"{op}: {response.text}"
        assert response.json() == example["response"]
        for name, mock in mocks.items():
            assert mock.await_count == (1 if name == op else 0)


@pytest.mark.asyncio
async def test_validation_and_safe_errors(client, monkeypatch):
    headers = {"X-Retro-Read-Token": "retro-secret"}
    mock = AsyncMock(side_effect=RuntimeError("secret exception"))
    monkeypatch.setitem(retro_v1.READERS, "data_gaps", mock)
    for query in (
        "?extra=x",
        "?limit=0",
        "?limit=501",
        "?cursor=%21",
        "?limit=1&limit=2",
    ):
        response = await client.get(
            f"{retro_v1.PREFIX}/data_gaps{query}", headers=headers
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
    mock.assert_not_awaited()
    response = await client.get(f"{retro_v1.PREFIX}/data_gaps", headers=headers)
    assert response.status_code == 503
    assert response.json() == {
        "code": "upstream_unavailable",
        "message": "Upstream unavailable.",
    }
    for code in (
        "validation_error",
        "timeout",
        "upstream_unavailable",
        "schema_mismatch",
    ):
        mock.side_effect = retro_v1.RetroReadError(code)
        response = await client.get(f"{retro_v1.PREFIX}/data_gaps", headers=headers)
        assert response.status_code == (422 if code == "validation_error" else 503)
        assert response.json()["code"] == code


@pytest.mark.asyncio
async def test_real_app_gates_and_dispatch(monkeypatch):
    from app.main import app as real_app

    monkeypatch.setattr(settings, "RETRO_API_ENABLED", True)
    monkeypatch.setattr(settings, "RETRO_API_READ_TOKEN", "retro-secret")
    mock = AsyncMock(return_value={"synthetic": True})
    monkeypatch.setitem(retro_v1.READERS, "data_gaps", mock)
    real_app.dependency_overrides[get_db] = lambda: object()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=real_app), base_url="http://test"
        ) as client:
            path = f"{retro_v1.PREFIX}/data_gaps"
            headers = {"X-Retro-Read-Token": "retro-secret"}
            assert (await client.post(path, headers=headers)).status_code == 403
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"synthetic": True}
            mock.assert_awaited_once()
    finally:
        real_app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oversized_limit_is_validation_error(client):
    response = await client.get(
        f"{retro_v1.PREFIX}/data_gaps",
        params={"limit": "9" * 5000},
        headers={"X-Retro-Read-Token": "retro-secret"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_market_query_validation(client, monkeypatch):
    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setitem(retro_v1.READERS, "get_bars", mock)
    headers = {"X-Retro-Read-Token": "retro-secret"}
    base = {"symbol": "BTC", "tf": "1d", "from": "2026-01-01", "to": "2026-01-01"}
    response = await client.get(
        f"{retro_v1.PREFIX}/get_bars",
        params={**base, "market": "other"},
        headers=headers,
    )
    assert response.status_code == 422
    assert mock.await_count == 0
    response = await client.get(
        f"{retro_v1.PREFIX}/get_bars", params={**base, "market": "us"}, headers=headers
    )
    assert response.status_code == 200
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_scoreboard_database_timeout_is_safe(client, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    class QueryCanceled(Exception):
        sqlstate = "57014"

    failure = DBAPIError("secret query", {}, QueryCanceled("secret detail"))
    monkeypatch.setitem(
        retro_v1.READERS, "retro_scoreboard", AsyncMock(side_effect=failure)
    )
    response = await client.get(
        f"{retro_v1.PREFIX}/retro_scoreboard",
        params={"from": "2026-01-01", "to": "2026-01-02"},
        headers={"X-Retro-Read-Token": "retro-secret"},
    )
    assert response.status_code == 503
    assert response.json() == {"code": "timeout", "message": "Request timed out."}
