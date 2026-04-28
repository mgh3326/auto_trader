"""Tests for research_run_decision_sessions router."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ENDPOINT = "/trading/api/decisions/from-research-run"


def _app() -> FastAPI:
    from app.routers import research_run_decision_sessions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(research_run_decision_sessions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    return app


def _candidate() -> SimpleNamespace:
    return SimpleNamespace(
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


def _run(*, candidates: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        run_uuid=uuid4(),
        market_scope="kr",
        candidates=[_candidate()] if candidates is None else candidates,
        reconciliations=[],
        source_warnings=[],
    )


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        refreshed_at=datetime.now(UTC),
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


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        session_uuid=uuid4(),
        user_id=7,
        source_profile="research_run",
        strategy_name=None,
        market_scope="kr",
        status="open",
        notes=None,
        market_brief={},
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        proposals=[],
    )


def _result(run: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        session=_session(),
        research_run=run,
        research_run_uuid=run.run_uuid,
        refreshed_at=datetime.now(UTC),
        proposal_count=1,
        reconciliation_count=0,
        warnings=(),
    )


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run: SimpleNamespace,
    snapshot: SimpleNamespace | None = None,
    create_side_effect: Exception | None = None,
) -> None:
    from app.services import (
        research_run_decision_session_service,
        research_run_live_refresh_service,
    )

    monkeypatch.setattr(
        research_run_decision_session_service,
        "resolve_research_run",
        AsyncMock(return_value=run),
    )
    monkeypatch.setattr(
        research_run_live_refresh_service,
        "build_live_refresh_snapshot",
        AsyncMock(return_value=snapshot or _snapshot()),
    )
    monkeypatch.setattr(
        research_run_decision_session_service,
        "create_decision_session_from_research_run",
        AsyncMock(side_effect=create_side_effect)
        if create_side_effect is not None
        else AsyncMock(return_value=_result(run)),
    )


def _post(client: TestClient, payload: dict) -> object:
    return client.post(ENDPOINT, json=payload)


@pytest.mark.unit
def test_create_decision_from_research_run_201_with_run_uuid(
    monkeypatch: pytest.MonkeyPatch,
):
    """POST /decisions/from-research-run returns 201 with valid run_uuid."""
    run = _run()
    _patch_services(monkeypatch, run=run)

    response = _post(
        TestClient(_app()),
        {"selector": {"run_uuid": str(run.run_uuid)}},
    )

    assert response.status_code == 201
    body = response.json()
    assert "session_uuid" in body
    assert body["proposal_count"] == 1
    assert body["research_run_uuid"] == str(run.run_uuid)
    assert "session_url" in body


@pytest.mark.unit
def test_create_decision_from_research_run_201_with_latest_selector(
    monkeypatch: pytest.MonkeyPatch,
):
    """POST /decisions/from-research-run returns 201 with market_scope+stage selector."""
    run = _run()
    _patch_services(monkeypatch, run=run)

    response = _post(
        TestClient(_app()),
        {"selector": {"market_scope": "kr", "stage": "preopen"}},
    )

    assert response.status_code == 201
    assert response.json()["proposal_count"] == 1


@pytest.mark.unit
def test_create_decision_404_unknown_uuid(monkeypatch: pytest.MonkeyPatch):
    """POST /decisions/from-research-run returns 404 for unknown run_uuid."""
    from app.services import research_run_decision_session_service

    monkeypatch.setattr(
        research_run_decision_session_service,
        "resolve_research_run",
        AsyncMock(
            side_effect=research_run_decision_session_service.ResearchRunNotFound(
                "Not found"
            )
        ),
    )

    response = _post(
        TestClient(_app()),
        {"selector": {"run_uuid": str(uuid4())}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "research_run_not_found"


@pytest.mark.unit
def test_create_decision_422_empty_candidates(monkeypatch: pytest.MonkeyPatch):
    """POST /decisions/from-research-run returns 422 for empty candidates."""
    from app.services import research_run_decision_session_service

    run = _run(candidates=[])
    _patch_services(
        monkeypatch,
        run=run,
        snapshot=_snapshot(),
        create_side_effect=research_run_decision_session_service.EmptyResearchRunError(
            "No candidates"
        ),
    )

    response = _post(
        TestClient(_app()),
        {"selector": {"run_uuid": str(run.run_uuid)}},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "research_run_has_no_candidates"


@pytest.mark.unit
def test_create_decision_501_tradingagents(monkeypatch: pytest.MonkeyPatch):
    """POST /decisions/from-research-run returns 501 for include_tradingagents=True."""
    run = _run()
    _patch_services(
        monkeypatch,
        run=run,
        snapshot=_snapshot(),
        create_side_effect=NotImplementedError("TradingAgents not supported"),
    )

    response = _post(
        TestClient(_app()),
        {
            "selector": {"run_uuid": str(run.run_uuid)},
            "include_tradingagents": True,
        },
    )

    assert response.status_code == 501
    assert response.json()["detail"] == "not_implemented"
