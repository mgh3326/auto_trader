"""Unit tests for app/routers/pagination.py Depends helpers."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.routers.pagination import (
    PageParams,
    PaginationParams,
    page_params,
    pagination_params,
)

# ---------------------------------------------------------------------------
# PaginationParams unit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pagination_params_defaults():
    dep = pagination_params(default_limit=20, max_limit=100)
    result: PaginationParams = dep(limit=None, offset=None)
    assert result.limit == 20
    assert result.offset == 0


@pytest.mark.unit
def test_pagination_params_explicit():
    dep = pagination_params(default_limit=20, max_limit=100)
    result: PaginationParams = dep(limit=40, offset=10)
    assert result.limit == 40
    assert result.offset == 10


@pytest.mark.unit
def test_pagination_params_clamps_to_max():
    dep = pagination_params(default_limit=20, max_limit=100)
    result: PaginationParams = dep(limit=9999, offset=0)
    assert result.limit == 100


@pytest.mark.unit
def test_pagination_params_rejects_negative_limit():
    app = FastAPI()
    dep = pagination_params(default_limit=20, max_limit=100)

    @app.get("/items")
    def list_items(p: PaginationParams = Depends(dep)):
        return {"limit": p.limit, "offset": p.offset}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/items?limit=-1&offset=0")
    assert resp.status_code == 422


@pytest.mark.unit
def test_pagination_params_rejects_negative_offset():
    app = FastAPI()
    dep = pagination_params(default_limit=20, max_limit=100)

    @app.get("/items")
    def list_items(p: PaginationParams = Depends(dep)):
        return {"limit": p.limit, "offset": p.offset}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/items?limit=10&offset=-1")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PageParams unit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_page_params_defaults():
    dep = page_params(default_size=50, max_size=200)
    result: PageParams = dep(page=None, page_size=None)
    assert result.page == 1
    assert result.page_size == 50


@pytest.mark.unit
def test_page_params_explicit():
    dep = page_params(default_size=50, max_size=200)
    result: PageParams = dep(page=3, page_size=100)
    assert result.page == 3
    assert result.page_size == 100


@pytest.mark.unit
def test_page_params_clamps_size():
    dep = page_params(default_size=50, max_size=200)
    result: PageParams = dep(page=1, page_size=9999)
    assert result.page_size == 200


# ---------------------------------------------------------------------------
# Via FastAPI Depends (full HTTP path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pagination_via_http_default():
    app = FastAPI()
    dep = pagination_params(default_limit=20, max_limit=100)

    @app.get("/items")
    def list_items(p: PaginationParams = Depends(dep)):
        return {"limit": p.limit, "offset": p.offset}

    client = TestClient(app)
    resp = client.get("/items")
    assert resp.status_code == 200
    assert resp.json() == {"limit": 20, "offset": 0}


@pytest.mark.unit
def test_pagination_via_http_explicit():
    app = FastAPI()
    dep = pagination_params(default_limit=20, max_limit=100)

    @app.get("/items")
    def list_items(p: PaginationParams = Depends(dep)):
        return {"limit": p.limit, "offset": p.offset}

    client = TestClient(app)
    resp = client.get("/items?limit=15&offset=30")
    assert resp.status_code == 200
    assert resp.json() == {"limit": 15, "offset": 30}


@pytest.mark.unit
def test_page_params_via_http():
    app = FastAPI()
    dep = page_params(default_size=50, max_size=200)

    @app.get("/stocks")
    def list_stocks(p: PageParams = Depends(dep)):
        return {"page": p.page, "page_size": p.page_size}

    client = TestClient(app)
    resp = client.get("/stocks?page=2&page_size=25")
    assert resp.status_code == 200
    assert resp.json() == {"page": 2, "page_size": 25}
