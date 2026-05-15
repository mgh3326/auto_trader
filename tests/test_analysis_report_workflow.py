"""ROB-257 analysis report persistence/API/MCP contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sample_create_payload() -> dict:
    return {
        "idempotency_key": "rob257-sample-1",
        "report_type": "preopen_action_plan",
        "market": "kr",
        "account_scope": "kis_live",
        "status": "draft",
        "summary": "삼성전자 후보 검토",
        "risk_summary": "확인 불가 항목은 주문으로 연결하지 않는다.",
        "data_freshness": {"cash_balance": "확인 불가"},
        "coverage": {"symbols": 1},
        "source_policy": [
            "KIS live is authoritative; Toss/Naver are cross-validation only."
        ],
        "safety_notes": ["decision artifact only; no broker order side effects"],
        "metadata": {"session": "sample"},
        "stage_results": [
            {
                "stage_key": "account_authority",
                "source": "kis_live",
                "status": "unavailable",
                "unavailable_reason": "확인 불가",
                "normalized_payload": {"cash": "확인 불가"},
                "warnings": ["do not infer cash"],
            }
        ],
        "candidates": [
            {
                "idempotency_key": "rob257-sample-1-005930-buy",
                "symbol": "005930",
                "market": "kr",
                "side": "buy",
                "action_type": "buy_candidate",
                "limit_price": "70000",
                "notional": "350000",
                "currency": "KRW",
                "priority": 10,
                "confidence": "0.7300",
                "thesis": "정규장 확인 후 수동 승인 후보",
                "risk_notes": ["정규장 확인 필요"],
                "verification": {"cash_balance": "확인 불가"},
                "blocking_reasons": ["cash_balance 확인 불가"],
                "approval_status": "awaiting_approval",
            }
        ],
    }


def _sample_report_response() -> dict:
    now = datetime(2026, 5, 15, 3, 0, tzinfo=UTC)
    return {
        "id": 1,
        "report_uuid": "00000000-0000-4000-8000-000000000001",
        "idempotency_key": "rob257-sample-1",
        "report_type": "preopen_action_plan",
        "market": "kr",
        "account_scope": "kis_live",
        "created_by_profile": "analyst",
        "status": "draft",
        "summary": "삼성전자 후보 검토",
        "risk_summary": "확인 불가 항목은 주문으로 연결하지 않는다.",
        "data_freshness": {"cash_balance": "확인 불가"},
        "coverage": {"symbols": 1},
        "source_policy": [
            "KIS live is authoritative; Toss/Naver are cross-validation only."
        ],
        "safety_notes": ["decision artifact only; no broker order side effects"],
        "metadata": {"session": "sample"},
        "created_at": now.isoformat(),
        "published_at": None,
        "valid_until": None,
        "stages": [
            {
                "id": 11,
                "stage_key": "account_authority",
                "source": "kis_live",
                "provenance": {},
                "status": "unavailable",
                "freshness_at": None,
                "raw_payload": None,
                "normalized_payload": {"cash": "확인 불가"},
                "unavailable_reason": "확인 불가",
                "warnings": ["do not infer cash"],
                "created_at": now.isoformat(),
            }
        ],
        "candidates": [
            {
                "id": 21,
                "candidate_uuid": "00000000-0000-4000-8000-000000000021",
                "report_uuid": "00000000-0000-4000-8000-000000000001",
                "idempotency_key": "rob257-sample-1-005930-buy",
                "symbol": "005930",
                "market": "kr",
                "side": "buy",
                "action_type": "buy_candidate",
                "quantity": None,
                "quantity_pct": None,
                "limit_price": "70000.0000",
                "notional": "350000.0000",
                "currency": "KRW",
                "priority": 10,
                "confidence": "0.7300",
                "thesis": "정규장 확인 후 수동 승인 후보",
                "risk_notes": ["정규장 확인 필요"],
                "verification": {"cash_balance": "확인 불가"},
                "blocking_reasons": ["cash_balance 확인 불가"],
                "approval_status": "awaiting_approval",
                "approval_type": "manual",
                "approved_by": None,
                "approved_at": None,
                "rejected_by": None,
                "rejected_at": None,
                "policy_id": None,
                "policy_snapshot": None,
                "execution_state": "not_submitted",
                "linked_trade_journal_id": None,
                "linked_order_ledger_ref": None,
                "created_at": now.isoformat(),
                "valid_until": None,
            }
        ],
        "idempotent": False,
    }


def test_analysis_models_define_decision_artifact_tables() -> None:
    from app.models.review import AnalysisOrderCandidate, AnalysisReport

    assert AnalysisReport.__table__.schema == "review"
    assert AnalysisReport.__tablename__ == "analysis_reports"
    assert "idempotency_key" in AnalysisReport.__table__.columns
    assert "execution_state" in AnalysisOrderCandidate.__table__.columns
    assert (
        AnalysisOrderCandidate.__table__.columns["execution_state"].default.arg
        == "not_submitted"
    )


def test_schema_preserves_unavailable_marker_and_rejects_bad_candidate_status() -> None:
    from pydantic import ValidationError

    from app.schemas.analysis_reports import AnalysisReportCreateRequest

    request = AnalysisReportCreateRequest.model_validate(_sample_create_payload())
    assert request.data_freshness["cash_balance"] == "확인 불가"
    assert request.candidates[0].execution_state == "not_submitted"

    bad = _sample_create_payload()
    bad["candidates"][0]["approval_status"] = "submitted_to_broker"
    with pytest.raises(ValidationError):
        AnalysisReportCreateRequest.model_validate(bad)


def test_router_create_and_read_contract_uses_service_without_broker_submission() -> (
    None
):
    from app.routers import analysis_reports as router_mod
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(router_mod.router)

    created = _sample_report_response()

    class StubService:
        async def create_report(self, request, *, created_by_profile: str):
            assert created_by_profile == "analyst"
            assert request.candidates[0].execution_state == "not_submitted"
            return created

        async def list_reports(self, **kwargs):
            assert kwargs == {"market": "kr", "status": "draft", "limit": 10}
            return {"count": 1, "items": [created]}

        async def get_report(self, report_uuid: str):
            return created if report_uuid == created["report_uuid"] else None

        async def list_candidates(self, **kwargs):
            return {"count": 1, "items": created["candidates"]}

        async def get_candidate(self, candidate_uuid: str):
            return created["candidates"][0]

    app.dependency_overrides[router_mod.get_analysis_report_service] = lambda: (
        StubService()
    )
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
        id=1, username="analyst"
    )

    with TestClient(app) as client:
        response = client.post(
            "/trading/api/analysis-reports", json=_sample_create_payload()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidates"][0]["execution_state"] == "not_submitted"
        assert body["data_freshness"]["cash_balance"] == "확인 불가"

        report_list_response = client.get(
            "/invest/api/action-center/reports?market=kr&status=draft&limit=10"
        )
        assert report_list_response.status_code == 200
        assert (
            report_list_response.json()["items"][0]["report_uuid"]
            == created["report_uuid"]
        )

        report_detail_response = client.get(
            f"/invest/api/action-center/reports/{created['report_uuid']}"
        )
        assert report_detail_response.status_code == 200
        assert report_detail_response.json()["report_uuid"] == created["report_uuid"]

        missing_report_response = client.get(
            "/invest/api/action-center/reports/00000000-0000-4000-8000-00000000ffff"
        )
        assert missing_report_response.status_code == 404

        read_response = client.get("/invest/api/action-center/candidates")
        assert read_response.status_code == 200
        assert (
            read_response.json()["items"][0]["approval_status"] == "awaiting_approval"
        )


@pytest.mark.asyncio
async def test_mcp_analysis_report_create_returns_decision_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mcp_server.tooling.analysis_reports_handlers as mod

    class StubService:
        async def create_report(self, request, *, created_by_profile: str):
            return _sample_report_response()

    fake_session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_session)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: cm)
    monkeypatch.setattr(mod, "AnalysisReportService", lambda db: StubService())

    result = await mod.analysis_report_create_impl(**_sample_create_payload())
    assert result["success"] is True
    assert result["report"]["candidates"][0]["execution_state"] == "not_submitted"
    assert result["idempotent"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("data_freshness", []),
        ("metadata", []),
        ("source_policy", ""),
        ("candidates", ""),
    ],
)
async def test_mcp_analysis_report_create_preserves_falsy_bad_types(
    field: str,
    bad_value: object,
) -> None:
    import app.mcp_server.tooling.analysis_reports_handlers as mod

    bad_payload = _sample_create_payload()
    bad_payload[field] = bad_value

    with pytest.raises(ValueError):
        await mod.analysis_report_create_impl(**bad_payload)
