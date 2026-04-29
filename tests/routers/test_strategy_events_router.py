from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _make_client():
    from app.routers import strategy_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(strategy_events.router)
    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    return TestClient(app), app


class _FakeDB:
    def __init__(self) -> None:
        self.commit = AsyncMock()


def _detail_stub(*, session_uuid=None):
    from app.schemas.strategy_events import StrategyEventDetail

    return StrategyEventDetail(
        id=1,
        event_uuid=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        session_uuid=session_uuid,
        source="user",
        event_type="operator_market_event",
        source_text="Fed hike",
        normalized_summary=None,
        affected_markets=["us"],
        affected_sectors=[],
        affected_themes=["macro"],
        affected_symbols=["AAPL"],
        severity=4,
        confidence=80,
        created_by_user_id=7,
        metadata=None,
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
    )


@pytest.mark.unit
def test_unauthenticated_post_returns_401():
    from app.core.db import get_db
    from app.routers import strategy_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(strategy_events.router)
    app.dependency_overrides[get_authenticated_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="auth required")
    )
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    client = TestClient(app)
    resp = client.post(
        "/trading/api/strategy-events",
        json={"event_type": "operator_market_event", "source_text": "x"},
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_authenticated_post_returns_201_and_round_trips_lists(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    create_mock = AsyncMock(return_value=_detail_stub())
    monkeypatch.setattr(strategy_event_service, "create_strategy_event", create_mock)

    fake_db = _FakeDB()
    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: fake_db

    payload = {
        "event_type": "operator_market_event",
        "source_text": "Fed hike",
        "affected_markets": ["us"],
        "affected_themes": ["macro"],
        "affected_symbols": ["AAPL"],
        "severity": 4,
        "confidence": 80,
    }
    resp = client.post("/trading/api/strategy-events", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["affected_markets"] == ["us"]
    assert body["affected_themes"] == ["macro"]
    assert body["affected_symbols"] == ["AAPL"]
    assert body["session_uuid"] is None
    assert body["event_uuid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fake_db.commit.assert_awaited_once()
    assert create_mock.await_args.kwargs["user_id"] == 7


@pytest.mark.unit
def test_authenticated_post_links_session_uuid(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    sess_uuid = uuid4()
    create_mock = AsyncMock(return_value=_detail_stub(session_uuid=sess_uuid))
    monkeypatch.setattr(strategy_event_service, "create_strategy_event", create_mock)

    fake_db = _FakeDB()
    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: fake_db

    resp = client.post(
        "/trading/api/strategy-events",
        json={
            "event_type": "operator_market_event",
            "source_text": "linked",
            "session_uuid": str(sess_uuid),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["session_uuid"] == str(sess_uuid)
    assert create_mock.await_args.kwargs["request"].session_uuid == sess_uuid


@pytest.mark.unit
def test_unknown_session_uuid_returns_404(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    create_mock = AsyncMock(
        side_effect=strategy_event_service.UnknownSessionUUIDError("x")
    )
    monkeypatch.setattr(strategy_event_service, "create_strategy_event", create_mock)

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()
    resp = client.post(
        "/trading/api/strategy-events",
        json={
            "event_type": "operator_market_event",
            "source_text": "x",
            "session_uuid": str(uuid4()),
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "session_uuid_not_found"


@pytest.mark.unit
def test_extra_fields_rejected_with_422():
    from app.core.db import get_db

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.post(
        "/trading/api/strategy-events",
        json={
            "event_type": "operator_market_event",
            "source_text": "x",
            "place_order": True,
        },
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_too_long_source_text_returns_422():
    from app.core.db import get_db

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.post(
        "/trading/api/strategy-events",
        json={"event_type": "operator_market_event", "source_text": "x" * 8001},
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_list_endpoint_filters_by_session_uuid(monkeypatch):
    from app.core.db import get_db
    from app.schemas.strategy_events import StrategyEventListResponse
    from app.services import strategy_event_service

    list_mock = AsyncMock(
        return_value=StrategyEventListResponse(
            events=[_detail_stub()], total=1, limit=50, offset=0
        )
    )
    monkeypatch.setattr(strategy_event_service, "list_strategy_events", list_mock)

    sess_uuid = uuid4()
    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.get(f"/trading/api/strategy-events?session_uuid={sess_uuid}&limit=10")
    assert resp.status_code == 200
    assert list_mock.await_args.kwargs["session_uuid"] == sess_uuid
    assert list_mock.await_args.kwargs["limit"] == 10


@pytest.mark.unit
def test_get_by_uuid_returns_404_when_absent(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    monkeypatch.setattr(
        strategy_event_service,
        "get_strategy_event_by_uuid",
        AsyncMock(return_value=None),
    )

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.get(f"/trading/api/strategy-events/{uuid4()}")
    assert resp.status_code == 404
