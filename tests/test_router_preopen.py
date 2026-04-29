"""Tests for the preopen dashboard router (ROB-39)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ENDPOINT = "/trading/api/preopen/latest"


def _app() -> FastAPI:
    from app.routers import preopen as preopen_router
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(preopen_router.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    return app


def _fail_open_response() -> SimpleNamespace:
    from app.schemas.preopen import PreopenLatestResponse

    return PreopenLatestResponse(
        has_run=False,
        advisory_skipped_reason="no_open_preopen_run",
        run_uuid=None,
        market_scope=None,
        stage=None,
        status=None,
        strategy_name=None,
        source_profile=None,
        generated_at=None,
        created_at=None,
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=0,
        reconciliation_count=0,
        candidates=[],
        reconciliations=[],
        linked_sessions=[],
    )


def _full_response() -> SimpleNamespace:
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.schemas.preopen import (
        CandidateSummary,
        PreopenLatestResponse,
        ReconciliationSummary,
    )

    return PreopenLatestResponse(
        has_run=True,
        advisory_skipped_reason=None,
        run_uuid=uuid4(),
        market_scope="kr",
        stage="preopen",
        status="open",
        strategy_name="Morning scan",
        source_profile="roadmap",
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=1,
        reconciliation_count=1,
        candidates=[
            CandidateSummary(
                candidate_uuid=uuid4(),
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                candidate_kind="proposed",
                proposed_price=Decimal("70000"),
                proposed_qty=Decimal("10"),
                confidence=75,
                rationale="Strong momentum",
                currency="KRW",
                warnings=[],
            )
        ],
        reconciliations=[
            ReconciliationSummary(
                order_id="ORD-1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="near_fill",
                nxt_classification="buy_pending_actionable",
                nxt_actionable=True,
                gap_pct=Decimal("0.50"),
                summary="Near fill",
                reasons=[],
                warnings=[],
            )
        ],
        linked_sessions=[],
    )


@pytest.mark.unit
def test_get_latest_preopen_unauthenticated_401():
    """Unauthenticated requests return 401."""
    from app.routers import preopen as preopen_router

    bare_app = FastAPI()
    bare_app.include_router(preopen_router.router)
    # No dependency override → real auth → 401
    client = TestClient(bare_app, raise_server_exceptions=False)
    response = client.get(ENDPOINT)
    assert response.status_code == 401


@pytest.mark.unit
def test_get_latest_preopen_returns_fail_open_payload(monkeypatch: pytest.MonkeyPatch):
    """GET /preopen/latest returns 200 with has_run=false when no run exists."""
    from app.services import preopen_dashboard_service

    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=_fail_open_response()),
    )

    response = TestClient(_app()).get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert body["has_run"] is False
    assert body["advisory_skipped_reason"] == "no_open_preopen_run"
    assert body["candidates"] == []
    assert body["linked_sessions"] == []


@pytest.mark.unit
def test_get_latest_preopen_with_run_returns_full_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    """GET /preopen/latest returns 200 with full payload when run exists."""
    from app.services import preopen_dashboard_service

    full = _full_response()
    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=full),
    )

    response = TestClient(_app()).get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert body["has_run"] is True
    assert body["candidate_count"] == 1
    assert body["reconciliation_count"] == 1
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["symbol"] == "005930"
    assert body["candidates"][0]["side"] == "buy"


@pytest.mark.unit
def test_market_scope_param_validation_rejects_us_for_now():
    """?market_scope=us returns 422 since only 'kr' is allowed now."""
    from app.services import preopen_dashboard_service

    app = _app()
    # Patch so we don't hit DB
    app.dependency_overrides[preopen_dashboard_service.get_latest_preopen_dashboard] = (
        lambda: _fail_open_response()
    )

    response = TestClient(app).get(f"{ENDPOINT}?market_scope=us")
    assert response.status_code == 422
