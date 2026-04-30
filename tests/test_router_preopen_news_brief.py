"""Router-level tests for the KR preopen news_brief field (ROB-62).

Verifies:
- GET /preopen/latest returns 200 with news_brief populated for each readiness state.
- Response is read-only: no DB writes occur (verified by spy on dashboard service mock).
- news_brief is advisory_only=True invariant.
- news_brief is present in 200 response for run-present and fail-open cases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.preopen_news_brief import (
    BriefConfidence,
    CandidateImpactFlag,
    KRPreopenNewsBrief,
    RiskFlag,
)

ENDPOINT = "/trading/api/preopen/latest"


def _app() -> FastAPI:
    from app.routers import preopen as preopen_router
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(preopen_router.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    return app


def _base_response(**kwargs):
    from app.schemas.preopen import PreopenLatestResponse

    defaults = {
        "has_run": True,
        "advisory_used": False,
        "advisory_skipped_reason": None,
        "run_uuid": None,
        "market_scope": "kr",
        "stage": "preopen",
        "status": "open",
        "strategy_name": None,
        "source_profile": "roadmap",
        "generated_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
        "notes": None,
        "market_brief": None,
        "source_freshness": None,
        "source_warnings": [],
        "advisory_links": [],
        "candidate_count": 0,
        "reconciliation_count": 0,
        "candidates": [],
        "reconciliations": [],
        "linked_sessions": [],
        "news": None,
        "news_preview": [],
        "news_brief": None,
    }
    defaults.update(kwargs)
    return PreopenLatestResponse(**defaults)


def _brief(
    readiness: str = "ok",
    overall: int = 80,
    cap_reason: str = "ok",
) -> KRPreopenNewsBrief:
    return KRPreopenNewsBrief(
        generated_at=datetime.now(UTC),
        news_readiness=readiness,  # type: ignore[arg-type]
        news_max_age_minutes=180,
        confidence=BriefConfidence(overall=overall, cap_reason=cap_reason),  # type: ignore[arg-type]
        sector_flags=[],
        candidate_flags=[],
        risk_flags=[],
        research_run_id=None,
        advisory_only=True,
    )


# --- Basic presence tests ---


@pytest.mark.unit
@pytest.mark.parametrize(
    "readiness,overall,cap_reason",
    [
        ("ok", 80, "ok"),
        ("stale", 55, "news_stale"),
        ("degraded", 35, "no_tradingagents_evidence"),
        ("unavailable", 0, "news_unavailable"),
    ],
)
def test_get_preopen_returns_200_with_news_brief_for_each_readiness(
    monkeypatch: pytest.MonkeyPatch,
    readiness: str,
    overall: int,
    cap_reason: str,
):
    """GET /preopen/latest returns 200 with news_brief for every readiness state."""
    from app.services import preopen_dashboard_service

    brief = _brief(readiness=readiness, overall=overall, cap_reason=cap_reason)
    response_obj = _base_response(news_brief=brief)

    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=response_obj),
    )

    response = TestClient(_app()).get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert "news_brief" in body
    assert body["news_brief"]["advisory_only"] is True
    assert body["news_brief"]["news_readiness"] == readiness
    assert body["news_brief"]["confidence"]["overall"] == overall


@pytest.mark.unit
def test_get_preopen_news_brief_none_when_fail_open(monkeypatch: pytest.MonkeyPatch):
    """Fail-open (no run) returns 200 with news_brief=null."""
    from app.schemas.preopen import PreopenLatestResponse
    from app.services import preopen_dashboard_service

    fail_open = PreopenLatestResponse(
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
        news=None,
        news_preview=[],
        news_brief=None,
    )
    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=fail_open),
    )

    response = TestClient(_app()).get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert body["news_brief"] is None
    assert body["has_run"] is False


@pytest.mark.unit
def test_news_brief_unavailable_returns_200_with_brief_marked_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """Unavailable readiness: response is still 200, brief has confidence.overall==0."""
    from app.services import preopen_dashboard_service

    brief = _brief(readiness="unavailable", overall=0, cap_reason="news_unavailable")
    brief.risk_flags.append(
        RiskFlag(
            code="news_unavailable", severity="warn", message="뉴스 신선도: unavailable"
        )
    )
    response_obj = _base_response(news_brief=brief)

    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=response_obj),
    )

    response = TestClient(_app()).get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert body["news_brief"]["news_readiness"] == "unavailable"
    assert body["news_brief"]["confidence"]["overall"] == 0
    assert body["news_brief"]["sector_flags"] == []
    assert body["news_brief"]["candidate_flags"] == []


# --- Read-only guarantee ---


@pytest.mark.unit
def test_get_preopen_does_not_call_persistence(monkeypatch: pytest.MonkeyPatch):
    """GET dashboard path must not call record_kr_preopen_news_brief."""
    from app.services import preopen_dashboard_service, research_run_service

    record_mock = AsyncMock()
    monkeypatch.setattr(
        research_run_service,
        "record_kr_preopen_news_brief",
        record_mock,
    )
    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=_base_response()),
    )

    TestClient(_app()).get(ENDPOINT)
    record_mock.assert_not_called()


# --- Candidate flag has no forbidden keys ---


@pytest.mark.unit
def test_candidate_flags_in_response_have_no_forbidden_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services import preopen_dashboard_service

    candidate_flag = CandidateImpactFlag(
        symbol="005930",
        name="삼성전자",
        direction="positive",
        confidence=70,
        sector="반도체",
        reasons=["Good momentum"],
        research_run_candidate_id=1,
    )
    brief = _brief()
    brief.candidate_flags.append(candidate_flag)
    response_obj = _base_response(news_brief=brief)

    monkeypatch.setattr(
        preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        AsyncMock(return_value=response_obj),
    )

    response = TestClient(_app()).get(ENDPOINT)
    body = response.json()
    for flag in body["news_brief"]["candidate_flags"]:
        forbidden = {
            "quantity",
            "price",
            "side",
            "order_type",
            "dry_run",
            "watch",
            "order_intent",
        }
        violations = forbidden & set(flag.keys())
        assert not violations, f"Forbidden keys in candidate_flag: {violations}"
