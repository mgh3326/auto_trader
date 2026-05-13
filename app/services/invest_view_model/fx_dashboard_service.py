"""ROB-216 — deterministic fixture service for /invest FX·macro dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.schemas.invest_fx_dashboard import (
    FxDashboardAfterVerification,
    FxDashboardCollectionItem,
    FxDashboardDefenseSignal,
    FxDashboardDisclaimer,
    FxDashboardEventsSection,
    FxDashboardEvidenceItem,
    FxDashboardForeignFlowSection,
    FxDashboardNewsSection,
    FxDashboardQuoteMetric,
    FxDashboardResponse,
    FxDashboardSourceFreshness,
    FxDashboardThreshold,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _distance_pct(*, level: float, spot: float) -> float:
    return round(((level - spot) / spot) * 100, 2)


async def build_fx_dashboard(*, as_of: datetime | None = None) -> FxDashboardResponse:
    """Return the initial read-only FX dashboard contract fixture.

    This K1 slice intentionally avoids live provider calls, broker clients,
    database writes, scheduler activation, and watch/order intent creation. Later
    ROB-217/218/220 lanes can replace individual deferred sources while keeping
    the sourceFreshness envelope stable.
    """
    resolved_as_of = as_of or _now()
    if resolved_as_of.tzinfo is None:
        resolved_as_of = resolved_as_of.replace(tzinfo=UTC)

    spot_updated_at = resolved_as_of - timedelta(minutes=3)
    stale_updated_at = resolved_as_of - timedelta(hours=12)
    spot = 1498.70

    freshness = [
        FxDashboardSourceFreshness(
            source="fixture_usdkrw_spot",
            label="USD/KRW 현물",
            dataState="fresh",
            updatedAt=spot_updated_at,
            staleAfterMinutes=10,
            warning=None,
        ),
        FxDashboardSourceFreshness(
            source="fixture_global_dollar",
            label="글로벌 달러 비교",
            dataState="stale",
            updatedAt=stale_updated_at,
            staleAfterMinutes=60,
            warning="DXY/CNH/JPY/EUR live provider는 ROB-217에서 연결",
        ),
        FxDashboardSourceFreshness(
            source="official_after_verification",
            label="사후 검증 자료",
            dataState="missing",
            updatedAt=None,
            staleAfterMinutes=None,
            warning="공식/딜러/NDF 근거가 없으면 확정 표현 금지",
        ),
    ]

    defense_evidence = [
        FxDashboardEvidenceItem(
            kind="price",
            labelKo="USD/KRW spot",
            value=f"{spot:.2f}",
            source="fixture_usdkrw_spot",
            dataState="fresh",
        )
    ]

    return FxDashboardResponse(
        asOf=resolved_as_of,
        dataState="partial",
        warnings=[
            "usdkrw_spot: fixture provider; live provider not wired",
            "DXY/CNH/JPY/EUR/NDF/flow/news/calendar providers are deferred to ROB-217/218/220",
        ],
        disclaimers=[
            FxDashboardDisclaimer(
                code="not_confirmed_intervention",
                severity="caution",
                textKo="이 신호는 방어성 매도/수급 의심을 정리한 참고 지표이며 당국의 확정 개입 근거가 아닙니다. 공식 발표·딜러 코멘트·NDF 등 사후 검증이 필요합니다.",
            )
        ],
        sourceFreshness=freshness,
        usdKrw=FxDashboardQuoteMetric(
            symbol="USDKRW",
            spot=spot,
            change=3.2,
            changePct=0.21,
            tone="up",
            updatedAt=spot_updated_at,
            source="fixture_usdkrw_spot",
        ),
        thresholds=[
            FxDashboardThreshold(
                level=1450,
                label="주의",
                distancePct=_distance_pct(level=1450, spot=spot),
                state="watch",
            ),
            FxDashboardThreshold(
                level=1500,
                label="심리적 저항/당국 경계",
                distancePct=_distance_pct(level=1500, spot=spot),
                state="near",
            ),
        ],
        defenseSignal=FxDashboardDefenseSignal(
            state="watch",
            score=42,
            confidence="low",
            labelKo="당국 경계감/방어성 수급 의심",
            summaryKo="1500원 부근 접근으로 경계 신호는 있으나 확정 개입 근거는 없습니다.",
            reasonsKo=[
                "1500원 근접",
                "글로벌 달러 비교 일부 미수집",
                "사후 검증 자료 없음",
            ],
            evidence=defense_evidence,
            notConfirmedIntervention=True,
            needsAfterVerification=True,
        ),
        globalDollar=[
            FxDashboardCollectionItem(
                symbol="DXY",
                label="달러인덱스",
                value=None,
                changePct=None,
                dataState="missing",
                source="deferred",
            ),
            FxDashboardCollectionItem(
                symbol="USDCNH",
                label="달러/위안",
                value=None,
                changePct=None,
                dataState="missing",
                source="deferred",
            ),
        ],
        krwCrosses=[
            FxDashboardCollectionItem(
                symbol="CNYKRW",
                label="위안/원",
                value=None,
                changePct=None,
                dataState="missing",
                source="deferred",
            ),
            FxDashboardCollectionItem(
                symbol="JPYKRW",
                label="엔/원",
                value=None,
                changePct=None,
                dataState="missing",
                source="deferred",
            ),
        ],
        foreignFlow=FxDashboardForeignFlowSection(
            dataState="missing",
            summaryKo="외국인 수급 연결은 후속 작업입니다.",
            items=[],
        ),
        news=FxDashboardNewsSection(
            dataState="missing",
            items=[],
            warning="FX/당국 발언 뉴스 필터는 ROB-220에서 연결",
        ),
        events=FxDashboardEventsSection(
            dataState="missing",
            items=[],
            warning="FX macro calendar linkage는 ROB-220에서 연결",
        ),
        afterVerification=FxDashboardAfterVerification(
            dataState="missing",
            officialEvidence=[],
            dealerEvidence=[],
            ndfEvidence=[],
            summaryKo="공식 발표·딜러 코멘트·NDF 근거가 확인되기 전까지 확정 개입으로 표현하지 않습니다.",
        ),
    )
