"""Tests for trading decisions router."""

import importlib
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def restore_trading_decision_service_module():
    """Undo direct service mocks so later loadfile workers see real persistence."""
    from app.services import trading_decision_service

    importlib.reload(trading_decision_service)
    yield
    importlib.reload(trading_decision_service)


# Helpers
def _make_test_client():
    """Create a test client with mocked dependencies."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)

    # Mock authenticated user
    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    return TestClient(app), app, fake_user


# ========== Authentication Tests ==========


@pytest.mark.unit
def test_authenticated_create_session(monkeypatch: pytest.MonkeyPatch):
    """POST /decisions returns 201 with session data when authenticated."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    # Mock the service
    mock_session = SimpleNamespace(
        id=1,
        session_uuid=uuid4(),
        user_id=7,
        source_profile="test_profile",
        strategy_name=None,
        market_scope=None,
        status="open",
        workflow_status=None,
        account_mode=None,
        automation=None,
        artifacts=None,
        notes=None,
        market_brief=None,
        generated_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        proposals=[],
    )
    monkeypatch.setattr(
        trading_decision_service,
        "create_decision_session",
        AsyncMock(return_value=mock_session),
    )
    monkeypatch.setattr(
        trading_decision_service,
        "get_session_by_uuid",
        AsyncMock(return_value=mock_session),
    )

    client = TestClient(app)

    response = client.post(
        "/trading/api/decisions",
        json={
            "source_profile": "test_profile",
            "generated_at": datetime.utcnow().isoformat(),
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert "session_uuid" in body
    assert body["source_profile"] == "test_profile"
    assert body["status"] == "open"
    assert "proposals" in body


@pytest.mark.unit
def test_unauthenticated_request_returns_401():
    """Unauthenticated requests return 401."""
    from fastapi import HTTPException

    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)

    def raise_unauthorized():
        raise HTTPException(status_code=401, detail="Authentication required")

    app.dependency_overrides[get_authenticated_user] = raise_unauthorized
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    client = TestClient(app)

    response = client.get("/trading/api/decisions")
    assert response.status_code == 401


# ========== Session Tests ==========


@pytest.mark.unit
def test_create_proposals_btc_eth_sol():
    """POST /decisions/{uuid}/proposals with 3 items returns 201 with all 3."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    session_uuid = uuid4()

    # Mock session
    mock_session = SimpleNamespace(
        id=1,
        session_uuid=session_uuid,
        user_id=7,
        status="open",
    )
    trading_decision_service.get_session_by_uuid = AsyncMock(return_value=mock_session)

    # Mock proposals - need all fields for _to_proposal_detail
    def make_mock_proposal(pid, symbol, kind, side):
        return SimpleNamespace(
            id=pid,
            proposal_uuid=uuid4(),
            session_id=1,
            symbol=symbol,
            instrument_type="crypto",
            proposal_kind=kind,
            side=side,
            user_response="pending",
            responded_at=None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            original_quantity=None,
            original_quantity_pct=None,
            original_amount=None,
            original_price=None,
            original_trigger_price=None,
            original_threshold_pct=None,
            original_currency=None,
            original_rationale=None,
            original_payload={"test": "data"},
            user_quantity=None,
            user_quantity_pct=None,
            user_amount=None,
            user_price=None,
            user_trigger_price=None,
            user_threshold_pct=None,
            user_note=None,
            actions=[],
            counterfactuals=[],
            outcomes=[],
        )

    mock_proposals = [
        make_mock_proposal(1, "BTC", "trim", "sell"),
        make_mock_proposal(2, "ETH", "add", "buy"),
        make_mock_proposal(3, "SOL", "pullback_watch", "none"),
    ]
    trading_decision_service.add_decision_proposals = AsyncMock(
        return_value=mock_proposals
    )
    trading_decision_service.get_proposal_by_uuid = AsyncMock(
        side_effect=mock_proposals
    )

    client = TestClient(app)

    response = client.post(
        f"/trading/api/decisions/{session_uuid}/proposals",
        json={
            "proposals": [
                {
                    "symbol": "BTC",
                    "instrument_type": "crypto",
                    "proposal_kind": "trim",
                    "side": "sell",
                    "original_quantity": "0.5",
                    "original_payload": {"test": "data"},
                },
                {
                    "symbol": "ETH",
                    "instrument_type": "crypto",
                    "proposal_kind": "add",
                    "side": "buy",
                    "original_payload": {"test": "data"},
                },
                {
                    "symbol": "SOL",
                    "instrument_type": "crypto",
                    "proposal_kind": "pullback_watch",
                    "side": "none",
                    "original_payload": {"test": "data"},
                },
            ]
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["proposals"]) == 3
    for p in body["proposals"]:
        assert p["user_response"] == "pending"


@pytest.mark.unit
def test_modify_btc_20_to_10_preserves_original():
    """Modify response updates user_* fields while preserving original_* fields."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    def make_full_mock_proposal():
        return SimpleNamespace(
            id=1,
            proposal_uuid=proposal_uuid,
            session_id=1,
            session=SimpleNamespace(status="open"),
            symbol="BTC",
            instrument_type="crypto",
            proposal_kind="trim",
            side="sell",
            user_response="modify",
            responded_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            original_quantity=None,
            original_quantity_pct=Decimal("20"),
            original_amount=None,
            original_price=None,
            original_trigger_price=None,
            original_threshold_pct=None,
            original_currency=None,
            original_rationale=None,
            original_payload={},
            user_quantity=None,
            user_quantity_pct=Decimal("10"),
            user_amount=None,
            user_price=None,
            user_trigger_price=None,
            user_threshold_pct=None,
            user_note=None,
            actions=[],
            counterfactuals=[],
            outcomes=[],
        )

    mock_proposal = make_full_mock_proposal()
    trading_decision_service.get_proposal_by_uuid = AsyncMock(
        return_value=mock_proposal
    )
    trading_decision_service.record_user_response = AsyncMock(
        return_value=mock_proposal
    )

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/respond",
        json={
            "response": "modify",
            "user_quantity_pct": "10",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["original_quantity_pct"] == "20"
    assert body["user_quantity_pct"] == "10"

    call_kwargs = trading_decision_service.record_user_response.await_args.kwargs
    assert call_kwargs["responded_at"] is not None


@pytest.mark.unit
def test_accept_btc_eth_defer_sol():
    """Test multiple proposals with different responses."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    def make_mock_proposal(proposal_uuid, symbol, response):
        return SimpleNamespace(
            id=1,
            proposal_uuid=proposal_uuid,
            session_id=1,
            session=SimpleNamespace(status="open"),
            symbol=symbol,
            instrument_type="crypto",
            proposal_kind="trim" if symbol == "BTC" else "add",
            side="sell" if symbol == "BTC" else "buy",
            user_response=response,
            responded_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            original_quantity=None,
            original_quantity_pct=None,
            original_amount=None,
            original_price=None,
            original_trigger_price=None,
            original_threshold_pct=None,
            original_currency=None,
            original_rationale=None,
            original_payload={},
            user_quantity=None,
            user_quantity_pct=None,
            user_amount=None,
            user_price=None,
            user_trigger_price=None,
            user_threshold_pct=None,
            user_note=None,
            actions=[],
            counterfactuals=[],
            outcomes=[],
        )

    # BTC accept
    btc_uuid = uuid4()
    mock_btc = make_mock_proposal(btc_uuid, "BTC", "accept")
    trading_decision_service.get_proposal_by_uuid = AsyncMock(return_value=mock_btc)
    trading_decision_service.record_user_response = AsyncMock(return_value=mock_btc)

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{btc_uuid}/respond",
        json={"response": "accept"},
    )
    assert response.status_code == 200
    assert response.json()["user_response"] == "accept"

    # ETH accept
    eth_uuid = uuid4()
    mock_eth = make_mock_proposal(eth_uuid, "ETH", "accept")
    trading_decision_service.get_proposal_by_uuid = AsyncMock(return_value=mock_eth)
    trading_decision_service.record_user_response = AsyncMock(return_value=mock_eth)

    response = client.post(
        f"/trading/api/proposals/{eth_uuid}/respond",
        json={"response": "accept"},
    )
    assert response.status_code == 200
    assert response.json()["user_response"] == "accept"

    # SOL defer
    sol_uuid = uuid4()
    mock_sol = make_mock_proposal(sol_uuid, "SOL", "defer")
    trading_decision_service.get_proposal_by_uuid = AsyncMock(return_value=mock_sol)
    trading_decision_service.record_user_response = AsyncMock(return_value=mock_sol)

    response = client.post(
        f"/trading/api/proposals/{sol_uuid}/respond",
        json={"response": "defer"},
    )
    assert response.status_code == 200
    assert response.json()["user_response"] == "defer"


# ========== Action Tests ==========


@pytest.mark.unit
def test_record_live_order_action():
    """POST /actions records a live order with external ID."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    mock_proposal = SimpleNamespace(
        id=1,
        proposal_uuid=proposal_uuid,
        session_id=1,
        session=SimpleNamespace(status="open"),
        symbol="BTC",
    )
    trading_decision_service.get_proposal_by_uuid = AsyncMock(
        return_value=mock_proposal
    )

    mock_action = SimpleNamespace(
        id=1,
        proposal_id=1,
        action_kind="live_order",
        external_order_id="KIS-12345",
        external_paper_id=None,
        external_watch_id=None,
        external_source="kis",
        payload_snapshot={"order": "data"},
        recorded_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    trading_decision_service.record_decision_action = AsyncMock(
        return_value=mock_action
    )

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/actions",
        json={
            "action_kind": "live_order",
            "external_order_id": "KIS-12345",
            "external_source": "kis",
            "payload_snapshot": {"order": "data"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["action_kind"] == "live_order"
    assert body["external_order_id"] == "KIS-12345"

    # Verify service was called with correct kwargs
    call_kwargs = trading_decision_service.record_decision_action.await_args.kwargs
    assert call_kwargs["external_order_id"] == "KIS-12345"
    assert call_kwargs["external_source"] == "kis"


@pytest.mark.unit
def test_record_watch_alert_action():
    """POST /actions records a watch alert with external watch ID."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    mock_proposal = SimpleNamespace(
        id=1,
        proposal_uuid=proposal_uuid,
        session_id=1,
        session=SimpleNamespace(status="open"),
        symbol="BTC",
    )
    trading_decision_service.get_proposal_by_uuid = AsyncMock(
        return_value=mock_proposal
    )

    mock_action = SimpleNamespace(
        id=1,
        proposal_id=1,
        action_kind="watch_alert",
        external_order_id=None,
        external_paper_id=None,
        external_watch_id="WATCH-12345",
        external_source="upbit",
        payload_snapshot={"alert": "data"},
        recorded_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    trading_decision_service.record_decision_action = AsyncMock(
        return_value=mock_action
    )

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/actions",
        json={
            "action_kind": "watch_alert",
            "external_watch_id": "WATCH-12345",
            "external_source": "upbit",
            "payload_snapshot": {"alert": "data"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["action_kind"] == "watch_alert"
    assert body["external_watch_id"] == "WATCH-12345"


@pytest.mark.unit
def test_action_no_external_id_returns_422():
    """Action requiring external ID but none provided returns 422."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    client = TestClient(app)

    # Try to create live_order without external ID
    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/actions",
        json={
            "action_kind": "live_order",
            "payload_snapshot": {"order": "data"},
        },
    )

    assert response.status_code == 422


# ========== Authorization Tests ==========


@pytest.mark.unit
def test_session_not_owned_returns_404():
    """Accessing another user's session returns 404 (not 403)."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    session_uuid = uuid4()

    # Service returns None when not found or not owned
    trading_decision_service.get_session_by_uuid = AsyncMock(return_value=None)

    client = TestClient(app)

    response = client.get(f"/trading/api/decisions/{session_uuid}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.unit
def test_proposal_not_owned_returns_404():
    """Accessing another user's proposal returns 404 (not 403)."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    # Service returns None when not found or not owned
    trading_decision_service.get_proposal_by_uuid = AsyncMock(return_value=None)

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/respond",
        json={"response": "accept"},
    )

    assert response.status_code == 404


# ========== Session State Tests ==========


@pytest.mark.unit
def test_create_proposals_on_archived_session_returns_409():
    """Adding proposals to archived session returns 409."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    session_uuid = uuid4()

    # Mock archived session
    mock_session = SimpleNamespace(
        id=1,
        session_uuid=session_uuid,
        user_id=7,
        status="archived",
    )
    trading_decision_service.get_session_by_uuid = AsyncMock(return_value=mock_session)

    client = TestClient(app)

    response = client.post(
        f"/trading/api/decisions/{session_uuid}/proposals",
        json={
            "proposals": [
                {
                    "symbol": "BTC",
                    "instrument_type": "crypto",
                    "proposal_kind": "trim",
                    "original_payload": {},
                }
            ]
        },
    )

    assert response.status_code == 409
    assert "not open" in response.json()["detail"].lower()


# ========== Validation Tests ==========


@pytest.mark.unit
def test_modify_without_user_fields_returns_422():
    """Modify response without any user_* fields returns 422."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/respond",
        json={"response": "modify"},  # No user_* fields
    )

    assert response.status_code == 422


# ========== Outcome Tests ==========


@pytest.mark.unit
def test_outcome_mark_invalid_track_combo_returns_422():
    """Outcome with accepted_live + counterfactual_id returns 422."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/outcomes",
        json={
            "track_kind": "accepted_live",
            "counterfactual_id": 42,
            "horizon": "1h",
            "price_at_mark": "50000",
            "marked_at": datetime.utcnow().isoformat(),
        },
    )

    assert response.status_code == 422


@pytest.mark.unit
def test_outcome_mark_duplicate_horizon_returns_409():
    """Duplicate outcome mark at same horizon returns 409."""
    from sqlalchemy.exc import IntegrityError

    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    mock_proposal = SimpleNamespace(
        id=1,
        proposal_uuid=proposal_uuid,
        session_id=1,
        session=SimpleNamespace(status="open"),
        symbol="BTC",
    )
    trading_decision_service.get_proposal_by_uuid = AsyncMock(
        return_value=mock_proposal
    )

    # Simulate unique constraint violation
    trading_decision_service.record_outcome_mark = AsyncMock(
        side_effect=IntegrityError("Duplicate", None, None)
    )

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/outcomes",
        json={
            "track_kind": "accepted_live",
            "horizon": "1h",
            "price_at_mark": "50000",
            "marked_at": datetime.utcnow().isoformat(),
        },
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


# ========== List/Pagination Tests ==========


@pytest.mark.unit
def test_get_session_analytics_happy_path():
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service
    from app.services.trading_decision_service import AggregatedOutcomeCell

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)

    session_uuid = uuid4()
    trading_decision_service.aggregate_session_outcomes = AsyncMock(
        return_value=[
            AggregatedOutcomeCell(
                track_kind="accepted_live",
                horizon="1h",
                outcome_count=3,
                proposal_count=2,
                mean_pnl_pct=Decimal("1.5"),
                sum_pnl_amount=Decimal("12.34"),
                latest_marked_at=datetime.now(UTC),
            )
        ]
    )

    client = TestClient(app)
    res = client.get(f"/trading/api/decisions/{session_uuid}/analytics")
    assert res.status_code == 200
    body = res.json()
    assert body["session_uuid"] == str(session_uuid)
    assert body["tracks"] == [
        "accepted_live",
        "accepted_paper",
        "rejected_counterfactual",
        "analyst_alternative",
        "user_alternative",
    ]
    assert body["horizons"] == ["1h", "4h", "1d", "3d", "7d", "final"]
    assert len(body["cells"]) == 1
    assert body["cells"][0]["mean_pnl_pct"] == pytest.approx("1.5")


@pytest.mark.unit
def test_get_session_analytics_returns_404_for_unknown_session():
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    trading_decision_service.aggregate_session_outcomes = AsyncMock(return_value=None)

    client = TestClient(app)
    res = client.get(f"/trading/api/decisions/{uuid4()}/analytics")
    assert res.status_code == 404


@pytest.mark.unit
def test_list_decisions_pagination():
    """GET /decisions returns paginated list with counts."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    mock_sessions = [
        (
            SimpleNamespace(
                id=1,
                session_uuid=uuid4(),
                source_profile="profile1",
                strategy_name=None,
                market_scope=None,
                status="open",
                workflow_status=None,
                account_mode=None,
                generated_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ),
            5,  # proposals_count
            2,  # pending_count
        ),
        (
            SimpleNamespace(
                id=2,
                session_uuid=uuid4(),
                source_profile="profile2",
                strategy_name="test_strategy",
                market_scope="KR",
                status="closed",
                workflow_status=None,
                account_mode=None,
                generated_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ),
            3,  # proposals_count
            0,  # pending_count
        ),
    ]
    trading_decision_service.list_user_sessions = AsyncMock(
        return_value=(mock_sessions, 10)
    )

    client = TestClient(app)

    response = client.get("/trading/api/decisions?limit=50&offset=0")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 10
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["sessions"]) == 2
    assert body["sessions"][0]["proposals_count"] == 5
    assert body["sessions"][0]["pending_count"] == 2


@pytest.mark.unit
def test_get_session_detail_includes_nested_actions_and_outcomes():
    """GET /decisions/{uuid} includes nested proposals with actions/outcomes."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    session_uuid = uuid4()
    proposal_uuid = uuid4()

    # Build nested structure
    mock_session = SimpleNamespace(
        id=1,
        session_uuid=session_uuid,
        user_id=7,
        source_profile="test",
        strategy_name=None,
        market_scope=None,
        status="open",
        workflow_status=None,
        account_mode=None,
        automation=None,
        artifacts=None,
        notes=None,
        market_brief={},
        generated_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        proposals=[
            SimpleNamespace(
                id=1,
                proposal_uuid=proposal_uuid,
                session_id=1,
                symbol="BTC",
                instrument_type="crypto",
                proposal_kind="trim",
                side="sell",
                user_response="accept",
                responded_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                original_quantity=Decimal("0.5"),
                original_quantity_pct=None,
                original_amount=None,
                original_price=None,
                original_trigger_price=None,
                original_threshold_pct=None,
                original_currency=None,
                original_rationale=None,
                original_payload={},
                user_quantity=None,
                user_quantity_pct=None,
                user_amount=None,
                user_price=None,
                user_trigger_price=None,
                user_threshold_pct=None,
                user_note=None,
                actions=[
                    SimpleNamespace(
                        id=1,
                        proposal_id=1,
                        action_kind="live_order",
                        external_order_id="KIS-12345",
                        external_paper_id=None,
                        external_watch_id=None,
                        external_source="kis",
                        payload_snapshot={"order": "data"},
                        recorded_at=datetime.utcnow(),
                        created_at=datetime.utcnow(),
                    )
                ],
                counterfactuals=[],
                outcomes=[
                    SimpleNamespace(
                        id=1,
                        proposal_id=1,
                        counterfactual_id=None,
                        track_kind="accepted_live",
                        horizon="1h",
                        price_at_mark=Decimal("50000"),
                        pnl_pct=Decimal("5.5"),
                        pnl_amount=Decimal("100"),
                        marked_at=datetime.utcnow(),
                        payload=None,
                        created_at=datetime.utcnow(),
                    )
                ],
            )
        ],
    )
    trading_decision_service.get_session_by_uuid = AsyncMock(return_value=mock_session)

    client = TestClient(app)

    response = client.get(f"/trading/api/decisions/{session_uuid}")

    assert response.status_code == 200
    body = response.json()
    assert len(body["proposals"]) == 1
    assert len(body["proposals"][0]["actions"]) == 1
    assert len(body["proposals"][0]["outcomes"]) == 1
    assert body["proposals"][0]["actions"][0]["action_kind"] == "live_order"
    assert body["proposals"][0]["outcomes"][0]["horizon"] == "1h"


# ========== Counterfactual Tests ==========


@pytest.mark.unit
def test_create_counterfactual_track():
    """POST /counterfactuals creates a counterfactual track."""
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user
    from app.services import trading_decision_service

    app = FastAPI()
    app.include_router(trading_decisions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    proposal_uuid = uuid4()

    mock_proposal = SimpleNamespace(
        id=1,
        proposal_uuid=proposal_uuid,
        session_id=1,
        session=SimpleNamespace(status="open"),
        symbol="SOL",
    )
    trading_decision_service.get_proposal_by_uuid = AsyncMock(
        return_value=mock_proposal
    )

    mock_counterfactual = SimpleNamespace(
        id=1,
        proposal_id=1,
        track_kind="rejected_counterfactual",
        baseline_price=Decimal("100"),
        baseline_at=datetime.utcnow(),
        quantity=Decimal("10"),
        payload={"test": "data"},
        notes=None,
        created_at=datetime.utcnow(),
    )
    trading_decision_service.create_counterfactual_track = AsyncMock(
        return_value=mock_counterfactual
    )

    client = TestClient(app)

    response = client.post(
        f"/trading/api/proposals/{proposal_uuid}/counterfactuals",
        json={
            "track_kind": "rejected_counterfactual",
            "baseline_price": "100",
            "baseline_at": datetime.utcnow().isoformat(),
            "quantity": "10",
            "payload": {"test": "data"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["track_kind"] == "rejected_counterfactual"
    assert body["baseline_price"] == "100"
    assert body["quantity"] == "10"


# ========== Schema Consistency Test ==========


@pytest.mark.unit
def test_session_analytics_response_serializes_decimal_strings():
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.schemas.trading_decisions import (
        SessionAnalyticsCell,
        SessionAnalyticsResponse,
    )

    payload = SessionAnalyticsResponse(
        session_uuid=uuid4(),
        generated_at=datetime.now(UTC),
        tracks=[
            "accepted_live",
            "accepted_paper",
            "rejected_counterfactual",
            "analyst_alternative",
            "user_alternative",
        ],
        horizons=["1h", "4h", "1d", "3d", "7d", "final"],
        cells=[
            SessionAnalyticsCell(
                track_kind="accepted_live",
                horizon="1h",
                outcome_count=2,
                proposal_count=2,
                mean_pnl_pct=Decimal("1.5"),
                sum_pnl_amount=Decimal("12.34"),
                latest_marked_at=datetime.now(UTC),
            )
        ],
    )
    body = payload.model_dump(mode="json")
    assert body["cells"][0]["mean_pnl_pct"] == pytest.approx("1.5")
    assert body["cells"][0]["sum_pnl_amount"] == pytest.approx("12.34")
    assert body["tracks"][0] == "accepted_live"


@pytest.mark.unit
def test_pydantic_literals_match_db_enums():
    """Verify Pydantic Literal types match SQLAlchemy Enum values."""
    from typing import get_args

    from app.models.trading import InstrumentType
    from app.models.trading_decision import (
        ActionKind,
        CommitteeAccountMode,
        OutcomeHorizon,
        ProposalKind,
        SessionStatus,
        TrackKind,
        UserResponse,
        WorkflowStatus,
    )
    from app.schemas.trading_decisions import (
        AccountModeLiteral,
        ActionKindLiteral,
        InstrumentTypeLiteral,
        OutcomeHorizonLiteral,
        ProposalKindLiteral,
        SessionStatusLiteral,
        TrackKindLiteral,
        UserResponseLiteral,
        WorkflowStatusLiteral,
    )

    assert set(InstrumentType) == set(get_args(InstrumentTypeLiteral))
    assert set(ProposalKind) == set(get_args(ProposalKindLiteral))
    assert set(UserResponse) == set(get_args(UserResponseLiteral))
    assert set(ActionKind) == set(get_args(ActionKindLiteral))
    assert set(TrackKind) == set(get_args(TrackKindLiteral))
    assert set(OutcomeHorizon) == set(get_args(OutcomeHorizonLiteral))
    assert set(SessionStatus) == set(get_args(SessionStatusLiteral))
    assert set(WorkflowStatus) == set(get_args(WorkflowStatusLiteral))
    assert set(CommitteeAccountMode) == set(get_args(AccountModeLiteral))
