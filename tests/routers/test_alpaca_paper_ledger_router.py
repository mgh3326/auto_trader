"""Tests for the read-only Alpaca Paper ledger router (ROB-84)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _make_fake_row(**kwargs):
    defaults = {
        "id": 1,
        "client_order_id": "test-client-001",
        "broker": "alpaca",
        "account_mode": "alpaca_paper",
        "lifecycle_state": "canceled",
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTCUSD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "instrument_type": "crypto",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "gtc",
        "requested_qty": "0.001",
        "requested_notional": None,
        "requested_price": "50000",
        "currency": "USD",
        "broker_order_id": "broker-id-001",
        "submitted_at": datetime(2026, 5, 3, 9, 0, tzinfo=UTC),
        "order_status": "canceled",
        "filled_qty": "0",
        "filled_avg_price": None,
        "cancel_status": "confirmed",
        "canceled_at": datetime(2026, 5, 3, 9, 5, tzinfo=UTC),
        "position_snapshot": {"qty": "0", "avg_entry_price": None},
        "reconcile_status": None,
        "reconciled_at": None,
        "briefing_artifact_run_uuid": uuid4(),
        "briefing_artifact_status": "ready",
        "qa_evaluator_status": "ready",
        "approval_bridge_generated_at": datetime(2026, 5, 3, 8, 0, tzinfo=UTC),
        "approval_bridge_status": "available",
        "candidate_uuid": uuid4(),
        "workflow_stage": "crypto_weekend",
        "purpose": "paper_plumbing_smoke",
        "notes": None,
        "error_summary": None,
        "created_at": datetime(2026, 5, 3, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 3, 9, 5, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _mock_db_for_row(row):
    """Return an AsyncSession-like mock that returns `row` from queries."""

    class _Scalars:
        def all(self):
            return [row] if row is not None else []

    class _Result:
        def scalar_one_or_none(self):
            return row

        def scalars(self):
            return _Scalars()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    db.commit = AsyncMock()
    return db


def _mock_db_for_rows(rows):
    """Return an AsyncSession-like mock that returns `rows` from queries."""

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
    db.commit = AsyncMock()
    return db


def _make_app_with_db(db):
    """Build FastAPI app with the ledger router and overridden dependencies."""
    from app.core.db import get_db
    from app.routers import alpaca_paper_ledger
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(alpaca_paper_ledger.router)

    fake_user = SimpleNamespace(id=1)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: db
    return app


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_recent_returns_200_with_items():
    row = _make_fake_row()
    db = _mock_db_for_rows([row])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["items"][0]["client_order_id"] == "test-client-001"
    assert data["items"][0]["lifecycle_state"] == "canceled"


@pytest.mark.unit
def test_list_recent_empty_returns_200_empty_list():
    db = _mock_db_for_rows([])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []


@pytest.mark.unit
def test_list_recent_lifecycle_state_filter_passes():
    row = _make_fake_row(lifecycle_state="canceled")
    db = _mock_db_for_rows([row])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        "/trading/api/alpaca-paper/ledger/recent?lifecycle_state=canceled"
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# get_ledger_by_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_by_id_returns_200():
    row = _make_fake_row(id=42)
    db = _mock_db_for_row(row)
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/42")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 42


@pytest.mark.unit
def test_get_by_id_missing_returns_404():
    db = _mock_db_for_row(None)
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# get_ledger_by_client_order_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_by_client_order_id_returns_200():
    row = _make_fake_row(client_order_id="my-order-abc")
    db = _mock_db_for_row(row)
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        "/trading/api/alpaca-paper/ledger/by-client-order-id/my-order-abc"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["client_order_id"] == "my-order-abc"


@pytest.mark.unit
def test_get_by_client_order_id_missing_returns_404():
    db = _mock_db_for_row(None)
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/by-client-order-id/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth — unauthenticated requests return 401
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unauthenticated_returns_401():
    from app.core.db import get_db
    from app.routers import alpaca_paper_ledger
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(alpaca_paper_ledger.router)

    def _raise_401():
        raise HTTPException(status_code=401)

    app.dependency_overrides[get_authenticated_user] = _raise_401
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trading/api/alpaca-paper/ledger/recent")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Only GET methods are exposed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_only_get_methods_on_ledger_router():
    from app.routers.alpaca_paper_ledger import router

    all_methods: list[str] = []
    for route in router.routes:
        if hasattr(route, "methods") and route.methods:
            all_methods.extend(route.methods)

    for method in all_methods:
        assert method.upper() == "GET", (
            f"Non-GET method found on ledger router: {method}"
        )

    assert len(all_methods) > 0, "No routes found on ledger router"
