"""Unit tests for the ROB-664 analysis-artifact read-only router.

Mirrors the ROB-663 ``test_invest_forecasts_router.py`` shape: a fresh
``FastAPI()`` app with ``invest_artifacts.router`` mounted, the auth/db
dependencies overridden, and the service class methods monkeypatched so the
tests stay pure unit (no DB, no network). ``@pytest.mark.unit`` only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _meta_row(**kw):
    now = datetime(2026, 7, 3, tzinfo=UTC)
    base = {
        "id": 1,
        "artifact_uuid": uuid4(),
        "market": "kr",
        "kind": "screening_ranking",
        "title": "t",
        "symbols": [],
        "as_of": now,
        "valid_until": None,
        "session_label": None,
        "correlation_id": None,
        "account_scope": None,
        "content_hash": None,
        "version": 1,
        "readiness_label": None,
        "payload_size_bytes": 2,
        "is_stale": False,
        "created_by": "claude",
        "created_at": now,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _make_client(monkeypatch, *, rows=None, one=None):
    from app.core.db import get_db
    from app.routers import invest_artifacts
    from app.routers.dependencies import get_authenticated_user
    from app.services.analysis_artifact import AnalysisArtifactService

    calls: dict = {}

    async def _fake_list(self, **kwargs):
        calls["list"] = kwargs
        return rows if rows is not None else []

    async def _fake_get(self, artifact_id):
        calls["get"] = artifact_id
        return one

    monkeypatch.setattr(AnalysisArtifactService, "list_artifacts", _fake_list)
    monkeypatch.setattr(AnalysisArtifactService, "get", _fake_get)

    app = FastAPI()
    app.include_router(invest_artifacts.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_list_defaults(monkeypatch):
    client, calls = _make_client(monkeypatch, rows=[_meta_row(title="a")])
    r = client.get("/trading/api/invest/artifacts/")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["artifacts"][0]["title"] == "a"
    assert "payload" not in body["artifacts"][0]  # list = metadata only
    assert calls["list"]["include_stale"] is False


@pytest.mark.unit
def test_list_forwards_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/artifacts/"
        "?market=us&kind=briefing&readiness_label=blocked&symbol=AAPL"
        "&include_stale=true&limit=5"
    )
    assert r.status_code == 200
    assert calls["list"]["market"] == "us"
    assert calls["list"]["kind"] == "briefing"
    assert calls["list"]["readiness_label"] == "blocked"
    assert calls["list"]["symbol"] == "AAPL"
    assert calls["list"]["include_stale"] is True
    assert calls["list"]["limit"] == 5


@pytest.mark.unit
def test_list_rejects_invalid_kind(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert client.get("/trading/api/invest/artifacts/?kind=bogus").status_code == 422


@pytest.mark.unit
def test_list_rejects_invalid_readiness(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get("/trading/api/invest/artifacts/?readiness_label=bogus").status_code
        == 422
    )


@pytest.mark.unit
def test_get_detail_includes_payload(monkeypatch):
    row = _meta_row(id=9)
    row.payload = {"k": "v"}
    client, calls = _make_client(monkeypatch, one=row)
    r = client.get("/trading/api/invest/artifacts/9")
    assert r.status_code == 200
    body = r.json()
    assert body["artifact"]["payload"] == {"k": "v"}
    assert calls["get"] == "9"


@pytest.mark.unit
def test_get_detail_404(monkeypatch):
    client, _ = _make_client(monkeypatch, one=None)
    assert client.get("/trading/api/invest/artifacts/9").status_code == 404


@pytest.mark.unit
def test_list_forwards_correlation_ids(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/artifacts/"
        "?market=us&correlation_id=live:kis_live:abc&correlation_id=live:kis_live:def"
    )
    assert r.status_code == 200
    assert calls["list"]["correlation_ids"] == [
        "live:kis_live:abc",
        "live:kis_live:def",
    ]
