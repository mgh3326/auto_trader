"""Tests for research_run_decision_sessions router."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_create_decision_from_research_run_201_with_run_uuid():
    """POST /decisions/from-research-run returns 201 with valid run_uuid."""
    from app.routers import research_run_decision_sessions
    from app.routers.dependencies import get_authenticated_user
    from app.services import (
        research_run_decision_session_service,
        research_run_live_refresh_service,
    )

    app = FastAPI()
    app.include_router(research_run_decision_sessions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    mock_run = SimpleNamespace(
        id=1,
        run_uuid=uuid4(),
        market_scope="kr",
        candidates=[
            SimpleNamespace(
                id=1,
                symbol="005930",
                candidate_kind="proposed",
                proposed_price=Decimal("70000"),
                proposed_qty=Decimal("10"),
                instrument_type="equity_kr",
                side="buy",
                payload={},
                warnings=[],
                source_freshness=None,
                rationale=None,
                currency="KRW",
            )
        ],
        reconciliations=[],
        source_warnings=[],
    )

    mock_session = SimpleNamespace(
        id=1,
        session_uuid=uuid4(),
        user_id=7,
        source_profile="research_run",
        strategy_name=None,
        market_scope="kr",
        status="open",
        notes=None,
        market_brief={},
        generated_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        proposals=[],
    )

    mock_result = SimpleNamespace(
        session=mock_session,
        research_run=mock_run,
        research_run_uuid=mock_run.run_uuid,
        refreshed_at=datetime.utcnow(),
        proposal_count=1,
        reconciliation_count=0,
        warnings=(),
    )

    mock_snapshot = SimpleNamespace(
        refreshed_at=datetime.utcnow(),
        quote_by_symbol={"005930": SimpleNamespace(price=Decimal("70000"))},
        orderbook_by_symbol={},
        kr_universe_by_symbol={
            "005930": SimpleNamespace(nxt_eligible=True, name="삼성전자")
        },
        cash_balances={},
        holdings_by_symbol={},
        pending_orders=[],
        warnings=[],
    )

    research_run_decision_session_service.resolve_research_run = AsyncMock(
        return_value=mock_run
    )
    research_run_live_refresh_service.build_live_refresh_snapshot = AsyncMock(
        return_value=mock_snapshot
    )
    research_run_decision_session_service.create_decision_session_from_research_run = (
        AsyncMock(return_value=mock_result)
    )

    client = TestClient(app)

    response = client.post(
        "/trading/api/decisions/from-research-run",
        json={
            "selector": {
                "run_uuid": str(mock_run.run_uuid),
            }
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert "session_uuid" in body
    assert body["proposal_count"] == 1
    assert body["research_run_uuid"] == str(mock_run.run_uuid)
    assert "session_url" in body


@pytest.mark.unit
def test_create_decision_from_research_run_201_with_latest_selector():
    """POST /decisions/from-research-run returns 201 with market_scope+stage selector."""
    from app.routers import research_run_decision_sessions
    from app.routers.dependencies import get_authenticated_user
    from app.services import (
        research_run_decision_session_service,
        research_run_live_refresh_service,
    )

    app = FastAPI()
    app.include_router(research_run_decision_sessions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    mock_run = SimpleNamespace(
        id=1,
        run_uuid=uuid4(),
        market_scope="kr",
        candidates=[
            SimpleNamespace(
                id=1,
                symbol="005930",
                candidate_kind="proposed",
                proposed_price=Decimal("70000"),
                proposed_qty=Decimal("10"),
                instrument_type="equity_kr",
                side="buy",
                payload={},
                warnings=[],
                source_freshness=None,
                rationale=None,
                currency="KRW",
            )
        ],
        reconciliations=[],
        source_warnings=[],
    )

    mock_session = SimpleNamespace(
        id=1,
        session_uuid=uuid4(),
        user_id=7,
        source_profile="research_run",
        strategy_name=None,
        market_scope="kr",
        status="open",
        notes=None,
        market_brief={},
        generated_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        proposals=[],
    )

    mock_result = SimpleNamespace(
        session=mock_session,
        research_run=mock_run,
        research_run_uuid=mock_run.run_uuid,
        refreshed_at=datetime.utcnow(),
        proposal_count=1,
        reconciliation_count=0,
        warnings=(),
    )

    mock_snapshot = SimpleNamespace(
        refreshed_at=datetime.utcnow(),
        quote_by_symbol={"005930": SimpleNamespace(price=Decimal("70000"))},
        orderbook_by_symbol={},
        kr_universe_by_symbol={
            "005930": SimpleNamespace(nxt_eligible=True, name="삼성전자")
        },
        cash_balances={},
        holdings_by_symbol={},
        pending_orders=[],
        warnings=[],
    )

    research_run_decision_session_service.resolve_research_run = AsyncMock(
        return_value=mock_run
    )
    research_run_live_refresh_service.build_live_refresh_snapshot = AsyncMock(
        return_value=mock_snapshot
    )
    research_run_decision_session_service.create_decision_session_from_research_run = (
        AsyncMock(return_value=mock_result)
    )

    client = TestClient(app)

    response = client.post(
        "/trading/api/decisions/from-research-run",
        json={
            "selector": {
                "market_scope": "kr",
                "stage": "preopen",
            }
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert "session_uuid" in body
    assert body["proposal_count"] == 1


@pytest.mark.unit
def test_create_decision_404_unknown_uuid():
    """POST /decisions/from-research-run returns 404 for unknown run_uuid."""
    from app.routers import research_run_decision_sessions
    from app.routers.dependencies import get_authenticated_user
    from app.services import research_run_decision_session_service

    app = FastAPI()
    app.include_router(research_run_decision_sessions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    research_run_decision_session_service.resolve_research_run = AsyncMock(
        side_effect=research_run_decision_session_service.ResearchRunNotFound(
            "Not found"
        )
    )

    client = TestClient(app)

    response = client.post(
        "/trading/api/decisions/from-research-run",
        json={
            "selector": {
                "run_uuid": str(uuid4()),
            }
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "research_run_not_found"


@pytest.mark.unit
def test_create_decision_422_empty_candidates():
    """POST /decisions/from-research-run returns 422 for empty candidates."""
    from app.routers import research_run_decision_sessions
    from app.routers.dependencies import get_authenticated_user
    from app.services import (
        research_run_decision_session_service,
        research_run_live_refresh_service,
    )

    app = FastAPI()
    app.include_router(research_run_decision_sessions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    mock_run = SimpleNamespace(
        id=1,
        run_uuid=uuid4(),
        market_scope="kr",
        candidates=[],
        reconciliations=[],
    )

    mock_snapshot = SimpleNamespace(
        refreshed_at=datetime.utcnow(),
        quote_by_symbol={},
        orderbook_by_symbol={},
        kr_universe_by_symbol={},
        cash_balances={},
        holdings_by_symbol={},
        pending_orders=[],
        warnings=[],
    )

    research_run_decision_session_service.resolve_research_run = AsyncMock(
        return_value=mock_run
    )
    research_run_live_refresh_service.build_live_refresh_snapshot = AsyncMock(
        return_value=mock_snapshot
    )
    research_run_decision_session_service.create_decision_session_from_research_run = (
        AsyncMock(
            side_effect=research_run_decision_session_service.EmptyResearchRunError(
                "No candidates"
            )
        )
    )

    client = TestClient(app)

    response = client.post(
        "/trading/api/decisions/from-research-run",
        json={
            "selector": {
                "run_uuid": str(mock_run.run_uuid),
            }
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "research_run_has_no_candidates"


@pytest.mark.unit
def test_create_decision_501_tradingagents():
    """POST /decisions/from-research-run returns 501 for include_tradingagents=True."""
    from app.routers import research_run_decision_sessions
    from app.routers.dependencies import get_authenticated_user
    from app.services import (
        research_run_decision_session_service,
        research_run_live_refresh_service,
    )

    app = FastAPI()
    app.include_router(research_run_decision_sessions.router)

    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user

    mock_run = SimpleNamespace(
        id=1,
        run_uuid=uuid4(),
        market_scope="kr",
        candidates=[
            SimpleNamespace(
                id=1,
                symbol="005930",
                candidate_kind="proposed",
                proposed_price=Decimal("70000"),
                proposed_qty=Decimal("10"),
                instrument_type="equity_kr",
                side="buy",
                payload={},
                warnings=[],
                source_freshness=None,
                rationale=None,
                currency="KRW",
            )
        ],
        reconciliations=[],
    )

    mock_snapshot = SimpleNamespace(
        refreshed_at=datetime.utcnow(),
        quote_by_symbol={},
        orderbook_by_symbol={},
        kr_universe_by_symbol={},
        cash_balances={},
        holdings_by_symbol={},
        pending_orders=[],
        warnings=[],
    )

    research_run_decision_session_service.resolve_research_run = AsyncMock(
        return_value=mock_run
    )
    research_run_live_refresh_service.build_live_refresh_snapshot = AsyncMock(
        return_value=mock_snapshot
    )
    research_run_decision_session_service.create_decision_session_from_research_run = (
        AsyncMock(side_effect=NotImplementedError("TradingAgents not supported"))
    )

    client = TestClient(app)

    response = client.post(
        "/trading/api/decisions/from-research-run",
        json={
            "selector": {
                "run_uuid": str(mock_run.run_uuid),
            },
            "include_tradingagents": True,
        },
    )

    assert response.status_code == 501
    assert response.json()["detail"] == "not_implemented"
