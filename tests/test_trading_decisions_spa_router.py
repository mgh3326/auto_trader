"""Unit tests for the Trading Decision Workspace SPA router (ROB-6)."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import trading_decisions_spa


def _make_client(tmp_path: Path, *, with_dist: bool) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "assets").mkdir()
    if with_dist:
        (dist / "index.html").write_text(
            "<!doctype html><html><head>"
            '<script type="module" src="/trading/decisions/assets/index-abc123.js"></script>'
            '</head><body><div id="root"></div></body></html>',
            encoding="utf-8",
        )
        (dist / "assets" / "index-abc123.js").write_text(
            "export const x = 1;",
            encoding="utf-8",
        )
        (dist / "assets" / "logo.svg").write_text("<svg/>", encoding="utf-8")

    trading_decisions_spa.DIST_DIR = dist
    trading_decisions_spa.INDEX_FILE = dist / "index.html"
    trading_decisions_spa.ASSETS_DIR = dist / "assets"

    app = FastAPI()
    app.include_router(trading_decisions_spa.router)
    return TestClient(app)


@pytest.mark.unit
def test_spa_index_returns_html_when_dist_present(tmp_path: Path) -> None:
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "/trading/decisions/assets/index-abc123.js" in res.text
    assert res.headers["cache-control"].startswith("no-cache")


@pytest.mark.unit
def test_spa_deep_link_falls_back_to_index(tmp_path: Path) -> None:
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/inbox/2026-04-27")
    assert res.status_code == 200
    assert '<div id="root">' in res.text


@pytest.mark.unit
def test_assets_path_serves_hashed_asset(tmp_path: Path) -> None:
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/assets/index-abc123.js")
    assert res.status_code == 200
    assert "export const x = 1;" in res.text


@pytest.mark.unit
def test_assets_path_404s_for_unknown_asset(tmp_path: Path) -> None:
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/assets/missing.js")
    assert res.status_code == 404


@pytest.mark.unit
def test_assets_path_rejects_traversal(tmp_path: Path) -> None:
    client = _make_client(tmp_path, with_dist=True)
    res = client.get("/trading/decisions/assets/..%2Fsecret.txt")
    assert res.status_code in (400, 404)


@pytest.mark.unit
def test_index_returns_503_when_dist_missing(tmp_path: Path) -> None:
    client = _make_client(tmp_path, with_dist=False)
    res = client.get("/trading/decisions/")
    assert res.status_code == 503
    assert "build missing" in res.text.lower()
    assert "npm run build" in res.text
