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
from app.schemas.invest_fx_dashboard import FxDashboardEvidenceItem, FxDashboardResponse
from app.services.invest_view_model.fx_dashboard_service import (
    DefaultFxDashboardProvider,
    FxProviderQuote,
    build_fx_dashboard,
)
from app.services.invest_view_model.fx_defense_signal import (
    DefenseScoringInput,
    _score_defense_signal,
    _threshold_state,
)


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


def _assert_no_confirmed_intervention_claim(payload: Any) -> None:
    text = _safe_text(payload)
    for unsafe_fragment in (
        "개입 확정",
        "정부 개입 확정",
        "당국 개입 확정",
        "정부가 방어",
        "당국이 방어",
    ):
        assert unsafe_fragment not in text


class _StubFxProvider:
    async def get_usdkrw(self, *, as_of: datetime) -> FxProviderQuote:
        return FxProviderQuote(
            symbol="USDKRW",
            label="USD/KRW 현물",
            value=1498.7,
            change=3.2,
            change_pct=0.21,
            updated_at=as_of,
            source="stub_usdkrw",
        )

    async def get_global_dollar(self, *, as_of: datetime) -> list[FxProviderQuote]:
        return [
            FxProviderQuote(
                symbol="DXY",
                label="달러인덱스",
                value=105.2,
                change=0.2,
                change_pct=0.25,
                updated_at=as_of,
                source="stub_global_dollar",
            ),
            FxProviderQuote(
                symbol="USDCNH",
                label="달러/위안",
                value=7.24,
                change=0.01,
                change_pct=0.16,
                updated_at=as_of,
                source="stub_global_dollar",
            ),
            FxProviderQuote(
                symbol="USDJPY",
                label="달러/엔",
                value=156.1,
                change=0.12,
                change_pct=0.08,
                updated_at=as_of,
                source="stub_global_dollar",
            ),
        ]

    async def get_krw_crosses(self, *, as_of: datetime) -> list[FxProviderQuote]:
        return [
            FxProviderQuote(
                symbol="CNYKRW",
                label="위안/원",
                value=207.0,
                change=-0.2,
                change_pct=-0.18,
                updated_at=as_of,
                source="stub_krw_crosses",
            ),
            FxProviderQuote(
                symbol="JPYKRW",
                label="엔/원",
                value=960.0,
                change=-0.1,
                change_pct=-0.11,
                updated_at=as_of,
                source="stub_krw_crosses",
            ),
            FxProviderQuote(
                symbol="EURKRW",
                label="유로/원",
                value=1620.0,
                change=0.1,
                change_pct=0.02,
                updated_at=as_of,
                source="stub_krw_crosses",
            ),
        ]


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
def test_fx_defense_signal_scores_near_1500_rejection_as_elevated() -> None:
    signal = _score_defense_signal(
        DefenseScoringInput(
            spot=1497.8,
            recent_high=1499.8,
            recent_close_or_last=1496.9,
            global_dollar_change_pct=0.25,
            usdcnh_change_pct=None,
            krw_cross_change_pcts={},
        )
    )

    assert signal.state == "elevated"
    assert 60 <= signal.score < 70
    assert signal.confidence == "medium"
    assert signal.reasonsKo[:3] == [
        "1500원 5원 이내 근접",
        "1500원 직전 상단 꼬리/되밀림",
        "글로벌 달러 강세 대비 USD/KRW 상단 제한",
    ]
    assert signal.notConfirmedIntervention is True
    assert signal.needsAfterVerification is True
    _assert_no_confirmed_intervention_claim(signal.model_dump())


@pytest.mark.unit
def test_fx_defense_signal_does_not_score_missing_cross_market_data_as_evidence() -> (
    None
):
    signal = _score_defense_signal(
        DefenseScoringInput(
            spot=1498.7,
            recent_high=None,
            recent_close_or_last=None,
            global_dollar_change_pct=None,
            usdcnh_change_pct=None,
            usd_jpy_change_pct=None,
            krw_cross_change_pcts={},
        )
    )

    assert signal.state == "watch"
    assert signal.score == 30
    assert signal.confidence == "low"
    assert "글로벌 달러 강세 대비 USD/KRW 상단 제한" not in signal.reasonsKo
    assert "글로벌 달러/원화 교차 비교 일부 미수집" in signal.reasonsKo
    assert any(
        item.kind == "missing_context" and item.dataState == "missing"
        for item in signal.evidence
    )


@pytest.mark.unit
def test_fx_defense_signal_requires_after_verification_for_high_score_without_confirming_intervention() -> (
    None
):
    signal = _score_defense_signal(
        DefenseScoringInput(
            spot=1500.2,
            recent_high=1502.0,
            recent_close_or_last=1499.6,
            global_dollar_change_pct=0.32,
            usdcnh_change_pct=0.22,
            krw_cross_change_pcts={"CNYKRW": -0.18, "JPYKRW": -0.11},
            authority_context=[
                FxDashboardEvidenceItem(
                    kind="authority_context",
                    labelKo="당국 경계 발언 context-only fixture",
                    value="변동성 경계 발언은 참고 맥락으로만 사용",
                    source="fixture_authority_context",
                    dataState="fresh",
                )
            ],
            after_verification_has_strong_evidence=False,
        )
    )

    assert signal.state == "after_verification_required"
    assert signal.score >= 70
    assert signal.confidence == "medium"
    assert signal.notConfirmedIntervention is True
    assert signal.needsAfterVerification is True
    assert "사후 검증" in signal.summaryKo
    _assert_no_confirmed_intervention_claim(signal.model_dump())


@pytest.mark.unit
def test_fx_defense_signal_strong_context_stays_cautious_without_missing_conflict() -> (
    None
):
    signal = _score_defense_signal(
        DefenseScoringInput(
            spot=1500.2,
            recent_high=1502.0,
            recent_close_or_last=1499.6,
            global_dollar_change_pct=0.32,
            krw_cross_change_pcts={"CNYKRW": -0.18, "JPYKRW": -0.11},
            authority_context=[
                FxDashboardEvidenceItem(
                    kind="authority_context",
                    labelKo="딜러/NDF 사후 검증 context fixture",
                    value="사후 근거 일부 확인 테스트",
                    source="fixture_authority_context",
                    dataState="fresh",
                )
            ],
            after_verification_has_strong_evidence=True,
        )
    )

    assert signal.confidence == "high"
    assert signal.notConfirmedIntervention is True
    assert "사후 검증 근거 일부 확인" in signal.reasonsKo
    assert "사후 검증 자료 없음" not in signal.reasonsKo
    _assert_no_confirmed_intervention_claim(signal.model_dump())


@pytest.mark.unit
def test_fx_threshold_state_for_1500_near_and_breached() -> None:
    assert _threshold_state(level=1500, spot=1488.8) == "near"
    assert _threshold_state(level=1500, spot=1488.7) == "watch"
    assert _threshold_state(level=1500, spot=1500.0) == "breached"
    assert _threshold_state(level=1500, spot=1501.2) == "breached"


@pytest.mark.unit
def test_fx_defense_signal_does_not_score_context_only_evidence() -> None:
    base = DefenseScoringInput(
        spot=1498.7,
        recent_high=None,
        recent_close_or_last=None,
        global_dollar_change_pct=None,
        krw_cross_change_pcts={},
    )
    with_context = DefenseScoringInput(
        spot=1498.7,
        recent_high=None,
        recent_close_or_last=None,
        global_dollar_change_pct=None,
        krw_cross_change_pcts={},
        news_context=[
            FxDashboardEvidenceItem(
                kind="news_context",
                labelKo="환율/당국 경계 뉴스 context-only fixture",
                value="참고 맥락",
                source="fixture_fx_news_context",
                dataState="stale",
            )
        ],
        authority_context=[
            FxDashboardEvidenceItem(
                kind="authority_context",
                labelKo="당국 경계 발언 context-only fixture",
                value="참고 맥락",
                source="fixture_authority_context",
                dataState="fresh",
            )
        ],
    )

    assert (
        _score_defense_signal(base).score == _score_defense_signal(with_context).score
    )
    assert "환율/당국 경계 뉴스 확인" in _score_defense_signal(with_context).reasonsKo


@pytest.mark.unit
def test_fx_defense_signal_requires_rejection_for_divergence_score() -> None:
    signal = _score_defense_signal(
        DefenseScoringInput(
            spot=1498.7,
            recent_high=None,
            recent_close_or_last=None,
            global_dollar_change_pct=0.25,
            usdcnh_change_pct=0.16,
            krw_cross_change_pcts={},
        )
    )

    assert signal.score == 30
    assert "글로벌 달러 강세 대비 USD/KRW 상단 제한" not in signal.reasonsKo


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_fx_dashboard_uses_live_provider_quotes() -> None:
    response = await build_fx_dashboard(
        as_of=datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC),
        provider=_StubFxProvider(),
    )

    assert response.dataState == "partial"
    assert response.usdKrw.value == 1498.7
    assert response.usdKrw.source == "stub_usdkrw"
    assert {item.symbol for item in response.globalDollar} == {
        "DXY",
        "USDCNH",
        "USDJPY",
    }
    assert {item.symbol for item in response.krwCrosses} == {
        "CNYKRW",
        "JPYKRW",
        "EURKRW",
    }
    assert response.news.dataState == "missing"
    assert response.events.dataState == "missing"
    assert response.afterVerification.dataState == "missing"
    assert response.defenseSignal.state == "after_verification_required"
    assert response.defenseSignal.notConfirmedIntervention is True
    assert response.defenseSignal.needsAfterVerification is True
    assert "1500원 1.5원 이내 근접" in response.defenseSignal.reasonsKo
    assert any(
        reason.startswith("1500원 부근 되밀림")
        for reason in response.defenseSignal.reasonsKo
    )
    assert "글로벌 달러 강세 대비 USD/KRW 상단 제한" in response.defenseSignal.reasonsKo
    assert "환율/당국 경계 뉴스 확인" in response.defenseSignal.reasonsKo
    _assert_no_confirmed_intervention_claim(response.model_dump())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_fx_provider_populates_global_dollar_from_raw_yahoo_seam() -> (
    None
):
    async def _fake_fast_info(ticker: str) -> dict[str, Any]:
        values = {
            "DX-Y.NYB": {"close": 105.2, "previous_close": 104.95},
            "CNH=X": {"close": 7.24, "previous_close": 7.22},
            "JPY=X": {"close": 156.1, "previous_close": 155.9},
            "EURUSD=X": {"close": 1.08, "previous_close": 1.079},
        }
        return values[ticker]

    provider = DefaultFxDashboardProvider(yahoo_fast_info_fetcher=_fake_fast_info)
    quotes = await provider.get_global_dollar(
        as_of=datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC)
    )

    by_symbol = {quote.symbol: quote for quote in quotes}
    assert by_symbol["DXY"].value == 105.2
    assert by_symbol["DXY"].change_pct == pytest.approx(0.2382, rel=1e-3)
    assert by_symbol["USDCNH"].source == "yahoo_global_dollar"
    assert by_symbol["USDCNH"].data_state == "fresh"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_fx_dashboard_degrades_when_optional_provider_sections_fail() -> (
    None
):
    class _FailingOptionalProvider(_StubFxProvider):
        async def get_global_dollar(self, *, as_of: datetime) -> list[FxProviderQuote]:
            raise RuntimeError("yahoo unavailable")

        async def get_krw_crosses(self, *, as_of: datetime) -> list[FxProviderQuote]:
            raise RuntimeError("naver crosses unavailable")

    response = await build_fx_dashboard(
        as_of=datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC),
        provider=_FailingOptionalProvider(),
    )

    assert response.usdKrw.value == 1498.7
    assert response.globalDollar
    assert all(item.dataState == "error" for item in response.globalDollar)
    assert response.krwCrosses
    assert all(item.dataState == "error" for item in response.krwCrosses)
    assert "yahoo_global_dollar: provider unavailable" in response.warnings
    assert "naver_marketindex_krw_crosses: provider unavailable" in response.warnings
    assert response.defenseSignal.notConfirmedIntervention is True
    _assert_no_confirmed_intervention_claim(response.model_dump())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()

    async def _stub_dashboard() -> FxDashboardResponse:
        return await build_fx_dashboard(
            as_of=datetime(2026, 5, 13, 8, 58, 16, tzinfo=UTC),
            provider=_StubFxProvider(),
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
        {
            "order_id",
            "client_order_id",
            "watch_order",
            "approval_issue_id",
            "order_intent",
        },
    )
    assert "dry_run=false" not in _safe_text(body)
    _assert_no_confirmed_intervention_claim(body)
