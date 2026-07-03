from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client(monkeypatch, *, list_result=None, na_result=None):
    from app.core.db import get_db
    from app.routers import invest_retrospectives
    from app.routers.dependencies import get_authenticated_user

    calls: dict = {}

    async def _fake_list(db, **kwargs):
        calls["list"] = kwargs
        return list_result or {"entries": [], "summary": {"count": 0, "total": 0}}

    async def _fake_na(db, **kwargs):
        calls["na"] = kwargs
        return na_result or {"items": [], "count": 0, "scan_limit": 200}

    monkeypatch.setattr(
        invest_retrospectives.retro_svc, "get_retrospectives", _fake_list
    )
    monkeypatch.setattr(
        invest_retrospectives.retro_svc, "get_open_next_actions", _fake_na
    )

    app = FastAPI()
    app.include_router(invest_retrospectives.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_list_defaults_all_market(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives")
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "all"
    assert body["total"] == 0
    # market="all" -> service market filter omitted (None)
    assert calls["list"]["market"] is None


@pytest.mark.unit
def test_list_forwards_filters_and_pagination(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/retrospectives"
        "?market=kr&trigger_type=fill&root_cause_class=analysis&limit=10&offset=20"
    )
    assert r.status_code == 200
    assert calls["list"]["market"] == "kr"
    assert calls["list"]["trigger_type"] == "fill"
    assert calls["list"]["root_cause_class"] == "analysis"
    assert calls["list"]["limit"] == 10
    assert calls["list"]["offset"] == 20


@pytest.mark.unit
def test_list_normalizes_us_symbol(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get("/trading/api/invest/retrospectives?market=us&symbol=BRK-B")
    assert r.status_code == 200
    assert calls["list"]["symbol"] == "BRK.B"  # to_db_symbol applied


@pytest.mark.unit
def test_list_rejects_invalid_enums(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get("/trading/api/invest/retrospectives?trigger_type=bogus").status_code
        == 422
    )
    assert (
        client.get(
            "/trading/api/invest/retrospectives?root_cause_class=bogus"
        ).status_code
        == 422
    )
    assert (
        client.get("/trading/api/invest/retrospectives?market=paper").status_code == 422
    )


@pytest.mark.unit
def test_list_maps_entries_to_items(monkeypatch):
    entry = {
        "id": 1,
        "correlation_id": "c",
        "symbol": "005930",
        "market": "kr",
        "instrument_type": "equity_kr",
        "trigger_type": "fill",
        "realized_pnl": 1000.0,
        "next_actions": [{"action": "x"}],
        "created_at": "2026-07-01T00:00:00+00:00",
        "outcome": "win",
        "extra_ignored_field": "dropped",
    }
    client, _ = _make_client(
        monkeypatch,
        list_result={"entries": [entry], "summary": {"count": 1, "total": 5}},
    )
    r = client.get("/trading/api/invest/retrospectives")
    body = r.json()
    assert body["count"] == 1
    assert body["total"] == 5
    assert body["items"][0]["symbol"] == "005930"
    assert "extra_ignored_field" not in body["items"][0]


@pytest.mark.unit
def test_next_actions_endpoint(monkeypatch):
    item = {
        "action": "재검토",
        "status": "open",
        "symbol": "005930",
        "market": "kr",
        "retro_id": 1,
        "correlation_id": "c",
        "trigger_type": "fill",
        "realized_pnl": None,
        "created_at": "2026-07-01T00:00:00+00:00",
        "owner": None,
        "issue_id": None,
        "due_kst_date": None,
    }
    client, calls = _make_client(
        monkeypatch,
        na_result={"items": [item], "count": 1, "scan_limit": 200},
    )
    r = client.get(
        "/trading/api/invest/retrospectives/next-actions?market=kr&symbol=005930"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["scan_limit"] == 200
    assert body["items"][0]["action"] == "재검토"
    assert calls["na"]["market"] == "kr"
    assert calls["na"]["symbol"] == "005930"


@pytest.mark.unit
def test_next_actions_status_csv_narrows(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/retrospectives/next-actions?status=open,in_progress"
    )
    assert r.status_code == 200
    assert calls["na"]["statuses"] == frozenset({"open", "in_progress"})


def _make_unauth_client():
    """Client with the real get_authenticated_user (no override) -> 401 path."""
    from app.core.db import get_db
    from app.routers import invest_retrospectives

    app = FastAPI()
    app.include_router(invest_retrospectives.router)
    # Only stub get_db; leave get_authenticated_user real so the cookieless
    # request resolves to HTTP 401 (state.user absent, no session cookie).
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app)


@pytest.mark.unit
def test_list_requires_authentication():
    client = _make_unauth_client()
    r = client.get("/trading/api/invest/retrospectives")
    assert r.status_code == 401
    assert r.json()["detail"] == "로그인이 필요합니다."


@pytest.mark.unit
def test_next_actions_requires_authentication():
    client = _make_unauth_client()
    r = client.get("/trading/api/invest/retrospectives/next-actions")
    assert r.status_code == 401
    assert r.json()["detail"] == "로그인이 필요합니다."

