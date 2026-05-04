"""Tests for the read-only watch order intent ledger router (ROB-103)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_fake_row(**overrides: object):
    base = {
        "id": 1,
        "correlation_id": "corr-router-1",
        "idempotency_key": (
            "kr:asset:005930:price_below:70000:create_order_intent:buy:2026-05-04"
        ),
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "condition_type": "price_below",
        "threshold": 70000.0,
        "threshold_key": "70000",
        "action": "create_order_intent",
        "side": "buy",
        "account_mode": "kis_mock",
        "execution_source": "watch",
        "lifecycle_state": "previewed",
        "quantity": 1.0,
        "limit_price": 70000.0,
        "notional": 70000.0,
        "currency": "KRW",
        "notional_krw_input": None,
        "max_notional_krw": 1500000.0,
        "notional_krw_evaluated": 70000.0,
        "fx_usd_krw_used": None,
        "approval_required": True,
        "execution_allowed": False,
        "blocking_reasons": [],
        "blocked_by": None,
        "detail": {},
        "preview_line": {"lifecycle_state": "previewed"},
        "triggered_value": 69000.0,
        "kst_date": "2026-05-04",
        "created_at": datetime(2026, 5, 4, 0, 30, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _mock_db_for_rows(rows):
    class _Scalars:
        def all(self):
            return rows

    class _Result:
        def scalar_one_or_none(self):
            return rows[0] if rows else None

        def scalars(self):
            return _Scalars()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    return db


def _make_app_with_db(db):
    from app.core.db import get_db
    from app.routers import watch_order_intent_ledger
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(watch_order_intent_ledger.router)
    fake_user = SimpleNamespace(id=1)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.mark.unit
def test_list_recent_returns_200_with_items():
    row = _make_fake_row()
    app = _make_app_with_db(_mock_db_for_rows([row]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["correlation_id"] == "corr-router-1"
    assert item["lifecycle_state"] == "previewed"
    assert item["account_mode"] == "kis_mock"
    assert item["execution_source"] == "watch"


@pytest.mark.unit
def test_list_recent_empty_returns_200_empty_list():
    app = _make_app_with_db(_mock_db_for_rows([]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []


@pytest.mark.unit
def test_get_by_correlation_returns_200_when_found():
    row = _make_fake_row(correlation_id="corr-found")
    app = _make_app_with_db(_mock_db_for_rows([row]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/corr-found")
    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == "corr-found"


@pytest.mark.unit
def test_get_by_correlation_404_when_missing():
    app = _make_app_with_db(_mock_db_for_rows([]))
    client = TestClient(app)

    resp = client.get("/trading/api/watch/order-intent/ledger/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not_found"
