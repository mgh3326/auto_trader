"""Tests for the read-only Alpaca Paper ledger router (ROB-84/ROB-90)."""

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
        "lifecycle_correlation_id": "test-client-001",
        "record_kind": "execution",
        "broker": "alpaca",
        "account_mode": "alpaca_paper",
        # ROB-90: canonical state (anomaly replaces old 'canceled')
        "lifecycle_state": "anomaly",
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
        "leg_role": None,
        "validation_attempt_no": None,
        "validation_outcome": None,
        "confirm_flag": True,
        "fee_amount": None,
        "fee_currency": None,
        "settlement_status": "n_a",
        "settlement_at": None,
        "qty_delta": None,
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
    # ROB-90: canonical state
    assert data["items"][0]["lifecycle_state"] == "anomaly"
    # ROB-90: new taxonomy fields present
    assert "lifecycle_correlation_id" in data["items"][0]
    assert "record_kind" in data["items"][0]


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
    row = _make_fake_row(lifecycle_state="anomaly")
    db = _mock_db_for_rows([row])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/recent?lifecycle_state=anomaly")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# get_ledger_by_correlation_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_by_correlation_id_returns_200():
    buy_row = _make_fake_row(
        client_order_id="buy-001",
        lifecycle_correlation_id="corr-abc",
        lifecycle_state="filled",
        side="buy",
    )
    sell_row = _make_fake_row(
        id=2,
        client_order_id="sell-001",
        lifecycle_correlation_id="corr-abc",
        lifecycle_state="closed",
        side="sell",
    )
    db = _mock_db_for_rows([buy_row, sell_row])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/by-correlation-id/corr-abc")
    assert resp.status_code == 200
    data = resp.json()
    assert data["lifecycle_correlation_id"] == "corr-abc"
    assert "count" in data
    assert "items" in data


@pytest.mark.unit
def test_get_by_correlation_id_empty_returns_200_zero_count():
    db = _mock_db_for_rows([])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get("/trading/api/alpaca-paper/ledger/by-correlation-id/no-such-corr")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["items"] == []


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
# ROB-92 roundtrip report endpoints
# ---------------------------------------------------------------------------


def _make_roundtrip_rows_for_router():
    buy_row = _make_fake_row(
        client_order_id="buy-rob92",
        lifecycle_correlation_id="corr-rob92",
        lifecycle_state="filled",
        side="buy",
        order_status="filled",
        filled_qty="0.001",
        filled_avg_price="50000",
        qty_delta="0.001",
        created_at=datetime(2026, 5, 3, 9, 1, tzinfo=UTC),
    )
    sell_row = _make_fake_row(
        id=2,
        client_order_id="sell-rob92",
        lifecycle_correlation_id="corr-rob92",
        lifecycle_state="closed",
        side="sell",
        order_status="filled",
        filled_qty="0.001",
        filled_avg_price="51000",
        qty_delta="-0.001",
        created_at=datetime(2026, 5, 3, 9, 2, tzinfo=UTC),
    )
    return [buy_row, sell_row]


@pytest.mark.unit
def test_roundtrip_report_by_correlation_id_returns_200():
    db = _mock_db_for_rows(_make_roundtrip_rows_for_router())
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        "/trading/api/alpaca-paper/roundtrip-report/by-correlation-id/corr-rob92"
        "?include_ledger_rows=false"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lookup_key"] == {
        "kind": "lifecycle_correlation_id",
        "value": "corr-rob92",
    }
    assert data["lifecycle_correlation_id"] == "corr-rob92"
    assert data["safety"]["read_only"] is True
    assert data["ledger_rows"] is None
    assert data["buy_leg"]["order"]["client_order_id"] == "buy-rob92"
    assert data["sell_leg"]["order"]["client_order_id"] == "sell-rob92"


@pytest.mark.unit
def test_roundtrip_report_by_correlation_id_missing_returns_404():
    db = _mock_db_for_rows([])
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        "/trading/api/alpaca-paper/roundtrip-report/by-correlation-id/missing"
    )

    assert resp.status_code == 404


@pytest.mark.unit
def test_roundtrip_report_by_client_order_id_returns_200():
    db = _mock_db_for_rows(_make_roundtrip_rows_for_router())
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        "/trading/api/alpaca-paper/roundtrip-report/by-client-order-id/buy-rob92"
        "?include_ledger_rows=false"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lookup_key"] == {"kind": "client_order_id", "value": "buy-rob92"}
    assert data["lifecycle_correlation_id"] == "corr-rob92"


@pytest.mark.unit
def test_roundtrip_report_by_candidate_uuid_returns_list_response():
    rows = _make_roundtrip_rows_for_router()
    candidate_uuid = rows[0].candidate_uuid
    db = _mock_db_for_rows(rows)
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        f"/trading/api/alpaca-paper/roundtrip-report/by-candidate-uuid/{candidate_uuid}"
        "?include_ledger_rows=false"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lookup_key"] == {
        "kind": "candidate_uuid",
        "value": str(candidate_uuid),
    }
    assert data["count"] == 1
    assert data["items"][0]["lifecycle_correlation_id"] == "corr-rob92"


@pytest.mark.unit
def test_roundtrip_report_by_briefing_artifact_run_uuid_returns_list_response():
    rows = _make_roundtrip_rows_for_router()
    briefing_uuid = rows[0].briefing_artifact_run_uuid
    db = _mock_db_for_rows(rows)
    app = _make_app_with_db(db)

    client = TestClient(app)
    resp = client.get(
        "/trading/api/alpaca-paper/roundtrip-report/"
        f"by-briefing-artifact-run-uuid/{briefing_uuid}?include_ledger_rows=false"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["lookup_key"] == {
        "kind": "briefing_artifact_run_uuid",
        "value": str(briefing_uuid),
    }
    assert data["count"] == 1


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
