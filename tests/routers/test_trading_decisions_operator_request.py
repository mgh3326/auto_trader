from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _make_test_client():
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)
    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    return TestClient(app), app


class _FakeDB:
    def __init__(self) -> None:
        self.commit = AsyncMock()


@pytest.mark.unit
def test_no_advisory_returns_201_with_session_url(monkeypatch):
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.services import operator_decision_session_service

    sess_uuid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    fake_result = SimpleNamespace(
        session=SimpleNamespace(id=1, session_uuid=sess_uuid, status="open"),
        proposal_count=1,
        advisory_used=False,
        advisory_skipped_reason="include_tradingagents=False",
    )
    create_mock = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        create_mock,
    )
    monkeypatch.setattr(
        trading_decisions.settings, "public_base_url", "https://trader.robinco.dev"
    )

    fake_db = _FakeDB()
    client, app = _make_test_client()
    app.dependency_overrides[get_db] = lambda: fake_db

    response = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "side": "buy",
                    "confidence": 70,
                    "proposal_kind": "enter",
                }
            ],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_uuid"] == str(sess_uuid)
    assert body["session_url"] == (
        f"https://trader.robinco.dev/trading/decisions/sessions/{sess_uuid}"
    )
    assert body["proposal_count"] == 1
    assert body["advisory_used"] is False
    assert body["advisory_skipped_reason"] == "include_tradingagents=False"
    assert response.headers["Location"] == f"/trading/api/decisions/{sess_uuid}"
    fake_db.commit.assert_awaited_once()
    create_mock.assert_awaited_once()
    assert create_mock.await_args.kwargs["user_id"] == 7


@pytest.mark.unit
def test_unauthenticated_returns_401():
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="auth required")
    )
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    client = TestClient(app)

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "confidence": 50,
                }
            ],
        },
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_extra_fields_rejected_with_422():
    from app.core.db import get_db

    client, app = _make_test_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "confidence": 50,
                }
            ],
            "place_order": True,
        },
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_tradingagents_not_configured_maps_to_503(monkeypatch):
    from app.core.db import get_db
    from app.services import operator_decision_session_service
    from app.services.tradingagents_research_service import TradingAgentsNotConfigured

    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(side_effect=TradingAgentsNotConfigured("missing")),
    )

    client, app = _make_test_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "us",
            "candidates": [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "confidence": 50,
                }
            ],
            "include_tradingagents": True,
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "tradingagents_not_configured"


@pytest.mark.unit
def test_tradingagents_runner_error_maps_to_502(monkeypatch):
    from app.core.db import get_db
    from app.services import operator_decision_session_service
    from app.services.tradingagents_research_service import TradingAgentsRunnerError

    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(side_effect=TradingAgentsRunnerError("crash")),
    )

    client, app = _make_test_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "us",
            "candidates": [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "confidence": 50,
                }
            ],
            "include_tradingagents": True,
        },
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "tradingagents_runner_failed"


@pytest.mark.unit
def test_response_session_url_falls_back_to_request_origin_when_unconfigured(
    monkeypatch,
):
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.services import operator_decision_session_service

    sess_uuid = uuid4()
    fake_result = SimpleNamespace(
        session=SimpleNamespace(id=1, session_uuid=sess_uuid, status="open"),
        proposal_count=1,
        advisory_used=False,
        advisory_skipped_reason="include_tradingagents=False",
    )
    monkeypatch.setattr(
        operator_decision_session_service,
        "create_operator_decision_session",
        AsyncMock(return_value=fake_result),
    )
    monkeypatch.setattr(trading_decisions.settings, "public_base_url", "")

    client, app = _make_test_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()

    resp = client.post(
        "/trading/api/decisions/from-operator-request",
        json={
            "market_scope": "kr",
            "candidates": [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "confidence": 50,
                }
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_url"].startswith("http://testserver/trading/decisions/sessions/")
    assert body["session_url"].endswith(str(sess_uuid))
