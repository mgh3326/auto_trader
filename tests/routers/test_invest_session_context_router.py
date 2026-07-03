"""Unit tests for the ROB-664 operator session-context read-only router.

Mirrors the ROB-663 router-unit pattern: fresh ``FastAPI()`` app with
``invest_session_context.router`` mounted, auth/db dependencies overridden,
``SessionContextService.get_recent`` monkeypatched. ``@pytest.mark.unit`` only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _entry(**kw):
    now = datetime(2026, 7, 3, tzinfo=UTC)
    base = {
        "entry_uuid": uuid4(),
        "kst_date": date(2026, 7, 3),
        "market": "kr",
        "account_scope": None,
        "entry_type": "handoff_note",
        "title": "t",
        "body": "b",
        "refs": {},
        "created_by": "claude",
        "session_label": None,
        "created_at": now,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _make_client(monkeypatch, *, rows=None):
    from app.core.db import get_db
    from app.routers import invest_session_context
    from app.routers.dependencies import get_authenticated_user
    from app.services.session_context import SessionContextService

    calls: dict = {}

    async def _fake_recent(self, **kwargs):
        calls["recent"] = kwargs
        return rows if rows is not None else []

    monkeypatch.setattr(SessionContextService, "get_recent", _fake_recent)

    app = FastAPI()
    app.include_router(invest_session_context.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: "db"
    return TestClient(app), calls


@pytest.mark.unit
def test_recent_defaults(monkeypatch):
    client, calls = _make_client(monkeypatch, rows=[_entry(title="a")])
    r = client.get("/trading/api/invest/session-context/recent")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["entries"][0]["title"] == "a"
    assert calls["recent"]["limit"] == 20


@pytest.mark.unit
def test_recent_forwards_filters(monkeypatch):
    client, calls = _make_client(monkeypatch)
    r = client.get(
        "/trading/api/invest/session-context/recent"
        "?market=us&account_scope=kis_live&entry_type=decision"
        "&kst_date_from=2026-07-01&limit=5"
    )
    assert r.status_code == 200
    assert calls["recent"]["market"] == "us"
    assert calls["recent"]["account_scope"] == "kis_live"
    assert calls["recent"]["entry_type"] == "decision"
    assert str(calls["recent"]["kst_date_from"]) == "2026-07-01"
    assert calls["recent"]["limit"] == 5


@pytest.mark.unit
def test_recent_rejects_invalid_entry_type(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get(
            "/trading/api/invest/session-context/recent?entry_type=bogus"
        ).status_code
        == 422
    )


@pytest.mark.unit
def test_recent_rejects_invalid_account_scope(monkeypatch):
    client, _ = _make_client(monkeypatch)
    assert (
        client.get(
            "/trading/api/invest/session-context/recent?account_scope=bogus"
        ).status_code
        == 422
    )
