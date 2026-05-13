from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.routers import invest_api
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_fx_dashboard import FxDashboardResponse
from app.services.invest_view_model.fx_dashboard_service import build_fx_dashboard


def _contains_key(payload: Any, forbidden: set[str]) -> bool:
    if isinstance(payload, dict):
        return any(
            key in forbidden or _contains_key(value, forbidden)
            for key, value in payload.items()
        )
    if isinstance(payload, list):
        return any(_contains_key(item, forbidden) for item in payload)
    return False


def _safe_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(_safe_text(value) for value in payload.values())
    if isinstance(payload, list):
        return " ".join(_safe_text(item) for item in payload)
    return ""


def _sample_payload() -> dict[str, Any]:
    return {
        "asOf": datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC),
        "dataState": "partial",
        "warnings": ["usdkrw_spot: fixture provider; live provider not wired"],
        "disclaimers": [
            {
                "code": "not_confirmed_intervention",
                "severity": "caution",
                "textKo": "이 신호는 방어성 매도/수급 의심을 정리한 참고 지표이며 당국 개입 확정 근거가 아닙니다. 공식 발표·딜러 코멘트·NDF 등 사후 검증이 필요합니다.",
            }
        ],
        "sourceFreshness": [
            {
                "source": "fixture_usdkrw_spot",
                "label": "USD/KRW 현물",
                "dataState": "fresh",
                "updatedAt": datetime(2026, 5, 13, 8, 55, tzinfo=UTC),
                "staleAfterMinutes": 10,
                "warning": None,
            },
            {
                "source": "official_after_verification",
                "label": "사후 검증 자료",
                "dataState": "missing",
                "updatedAt": None,
                "staleAfterMinutes": None,
                "warning": "공식/딜러/NDF 근거가 없으면 확정 표현 금지",
            },
        ],
        "usdKrw": {
            "symbol": "USDKRW",
            "spot": 1498.7,
            "change": 3.2,
            "changePct": 0.21,
            "tone": "up",
            "updatedAt": datetime(2026, 5, 13, 8, 55, tzinfo=UTC),
            "source": "fixture_usdkrw_spot",
        },
        "thresholds": [
            {"level": 1450, "label": "주의", "distancePct": 3.36, "state": "watch"},
            {
                "level": 1500,
                "label": "심리적 저항/당국 경계",
                "distancePct": -0.09,
                "state": "near",
            },
        ],
        "defenseSignal": {
            "state": "watch",
            "score": 42,
            "confidence": "low",
            "labelKo": "당국 경계감/방어성 수급 의심",
            "summaryKo": "1500원 부근 접근으로 경계 신호는 있으나 확정 개입 근거는 없습니다.",
            "reasonsKo": [
                "1500원 근접",
                "글로벌 달러 비교 일부 미수집",
                "사후 검증 자료 없음",
            ],
            "evidence": [
                {
                    "kind": "price",
                    "labelKo": "USD/KRW spot",
                    "value": "1498.70",
                    "source": "fixture_usdkrw_spot",
                    "dataState": "fresh",
                }
            ],
            "notConfirmedIntervention": True,
            "needsAfterVerification": True,
        },
        "globalDollar": [
            {
                "symbol": "DXY",
                "label": "달러인덱스",
                "value": None,
                "changePct": None,
                "dataState": "missing",
                "source": "deferred",
            }
        ],
        "krwCrosses": [
            {
                "symbol": "CNYKRW",
                "label": "위안/원",
                "value": None,
                "changePct": None,
                "dataState": "missing",
                "source": "deferred",
            }
        ],
        "foreignFlow": {
            "dataState": "missing",
            "summaryKo": "외국인 수급 연결은 후속 작업입니다.",
            "items": [],
        },
        "news": {
            "dataState": "missing",
            "items": [],
            "warning": "FX/당국 발언 뉴스 필터는 ROB-220에서 연결",
        },
        "events": {
            "dataState": "missing",
            "items": [],
            "warning": "FX macro calendar linkage는 ROB-220에서 연결",
        },
        "afterVerification": {
            "dataState": "missing",
            "officialEvidence": [],
            "dealerEvidence": [],
            "ndfEvidence": [],
            "summaryKo": "공식 발표·딜러 코멘트·NDF 근거가 확인되기 전까지 확정 개입으로 표현하지 않습니다.",
        },
    }


@pytest.mark.unit
def test_fx_dashboard_schema_forbids_unknown_fields() -> None:
    payload = _sample_payload()
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        FxDashboardResponse.model_validate(payload)


@pytest.mark.unit
def test_fx_dashboard_contract_contains_cautious_disclaimer_shape() -> None:
    response = FxDashboardResponse.model_validate(_sample_payload())

    assert response.defenseSignal.notConfirmedIntervention is True
    assert response.defenseSignal.needsAfterVerification is True
    assert response.disclaimers[0].code == "not_confirmed_intervention"
    caution_copy = response.disclaimers[0].textKo
    assert "확정" in caution_copy
    assert "아닙니다" in caution_copy


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_fx_dashboard_fixture_has_partial_provider_states() -> None:
    response = await build_fx_dashboard(
        as_of=datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC)
    )

    assert response.dataState == "partial"
    freshness_states = {item.dataState for item in response.sourceFreshness}
    assert "fresh" in freshness_states
    assert freshness_states & {"missing", "stale"}
    assert 0 <= response.defenseSignal.score <= 100
    assert response.news.dataState == "missing"
    assert response.events.dataState == "missing"
    assert response.afterVerification.dataState == "missing"
    assert "개입 확정" not in _safe_text(response.model_dump())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()

    async def _stub_dashboard() -> FxDashboardResponse:
        return await build_fx_dashboard(
            as_of=datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC)
        )

    monkeypatch.setattr(invest_api, "build_fx_dashboard", _stub_dashboard)
    return TestClient(app)


@pytest.mark.unit
def test_get_fx_dashboard_returns_read_only_payload(client: TestClient) -> None:
    response = client.get("/invest/api/market/fx/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["dataState"] == "partial"
    assert body["sourceFreshness"]
    assert body["defenseSignal"]["notConfirmedIntervention"] is True
    assert body["defenseSignal"]["needsAfterVerification"] is True
    assert body["disclaimers"][0]["code"] == "not_confirmed_intervention"
    assert not _contains_key(
        body,
        {"order_id", "client_order_id", "watch_order", "approval_issue_id"},
    )
