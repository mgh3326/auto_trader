"""ROB-147: Pydantic schema contract tests for /invest/api/screener/*."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.invest_screener import (
    ScreenerFilterChip,
    ScreenerFreshness,
    ScreenerPreset,
    ScreenerPresetsResponse,
    ScreenerResultRow,
    ScreenerResultsResponse,
)


@pytest.mark.unit
def test_preset_minimal_valid() -> None:
    preset = ScreenerPreset(
        id="consecutive_gainers",
        name="연속 상승세",
        description="일주일 연속 상승세를 보이는 주식",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),
            ScreenerFilterChip(label="주가 연속상승", detail="5일 이상 연속"),
        ],
        metricLabel="주가등락률",
        market="kr",
    )
    assert preset.id == "consecutive_gainers"
    assert preset.market == "kr"


@pytest.mark.unit
def test_preset_rejects_unknown_market() -> None:
    with pytest.raises(ValidationError):
        ScreenerPreset(
            id="x",
            name="x",
            description="x",
            badges=[],
            filterChips=[],
            metricLabel="x",
            market="forex",  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_results_response_with_warning_and_missing_metric() -> None:
    row = ScreenerResultRow(
        rank=1,
        symbol="005930",
        market="kr",
        name="삼성전자",
        logoUrl=None,
        isWatched=False,
        priceLabel="80,000원",
        changePctLabel="+1.23%",
        changeAmountLabel="+970",
        changeDirection="up",
        category="반도체",
        marketCapLabel="478조원",
        volumeLabel="12,345,678",
        analystLabel="-",
        metricValueLabel="-",
        warnings=["애널리스트 분석 데이터 준비중"],
    )
    resp = ScreenerResultsResponse(
        presetId="consecutive_gainers",
        title="연속 상승세",
        description="일주일 연속 상승세를 보이는 주식",
        filterChips=[
            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),
        ],
        metricLabel="주가등락률",
        results=[row],
        warnings=[],
        freshness=ScreenerFreshness(
            fetchedAt="2026-05-10T05:30:00+00:00",
            asOfLabel="2026.05.10 14:30 기준",
            relativeLabel="12분 전 갱신",
            cacheHit=False,
            source="live",
        ),
    )
    assert resp.results[0].symbol == "005930"
    assert resp.results[0].changeDirection == "up"


@pytest.mark.unit
def test_results_rejects_negative_rank() -> None:
    with pytest.raises(ValidationError):
        ScreenerResultRow(
            rank=0,
            symbol="x",
            market="kr",
            name="x",
            logoUrl=None,
            isWatched=False,
            priceLabel="-",
            changePctLabel="-",
            changeAmountLabel="-",
            changeDirection="flat",
            category="-",
            marketCapLabel="-",
            volumeLabel="-",
            analystLabel="-",
            metricValueLabel="-",
            warnings=[],
        )


@pytest.mark.unit
def test_screener_freshness_requires_all_fields() -> None:
    f = ScreenerFreshness(
        fetchedAt="2026-05-10T05:30:00+00:00",
        asOfLabel="2026.05.10 14:30 기준",
        relativeLabel="12분 전 갱신",
        cacheHit=False,
        source="live",
    )
    assert f.source == "live"


@pytest.mark.unit
def test_screener_freshness_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ScreenerFreshness(
            fetchedAt="2026-05-10T05:30:00+00:00",
            asOfLabel="x",
            relativeLabel="x",
            cacheHit=True,
            source="live",
            unexpected="nope",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_screener_results_response_requires_freshness() -> None:
    with pytest.raises(ValidationError):
        ScreenerResultsResponse(  # type: ignore[call-arg]
            presetId="consecutive_gainers",
            title="연속 상승세",
            description="",
            filterChips=[],
            metricLabel="연속상승",
            results=[],
            warnings=[],
        )


@pytest.mark.unit
def test_presets_response_holds_selected_id() -> None:
    preset = ScreenerPreset(
        id="cheap_value",
        name="아직 저렴한 가치주",
        description="x",
        badges=[],
        filterChips=[],
        metricLabel="PER",
        market="kr",
    )
    resp = ScreenerPresetsResponse(presets=[preset], selectedPresetId="cheap_value")
    assert resp.selectedPresetId == "cheap_value"


@pytest.mark.unit
def test_screener_freshness_accepts_new_primary_and_dependencies_fields() -> None:
    from app.schemas.invest_screener import (
        ScreenerFreshness,
        ScreenerFreshnessDependency,
        ScreenerFreshnessPrimary,
    )

    payload = ScreenerFreshness(
        fetchedAt="2026-05-20T00:10:00+00:00",
        asOfLabel="2026.05.13 장마감 기준",
        relativeLabel="5거래일 지연",
        cacheHit=True,
        source="cached",
        dataState="stale",
        servedAt="2026-05-20T00:10:00+00:00",
        servedRelativeLabel="방금",
        primary=ScreenerFreshnessPrimary(
            kind="screener_snapshot",
            snapshotDate="2026-05-13",
            computedAt="2026-05-13T06:35:00+00:00",
            asOfLabel="2026.05.13 장마감 기준",
            dataState="stale",
            source="invest_screener_snapshots",
        ),
        dependencies=[
            ScreenerFreshnessDependency(
                kind="investor_flow",
                snapshotDate="2026-05-18",
                collectedAt="2026-05-18T07:30:00+00:00",
                lagLabel="2거래일 지연",
                dataState="stale",
                source="investor_flow_snapshots",
            )
        ],
        overallState="stale",
    )
    assert payload.primary is not None
    assert payload.primary.kind == "screener_snapshot"
    assert payload.dependencies[0].kind == "investor_flow"
    assert payload.overallState == payload.dataState


@pytest.mark.unit
def test_screener_freshness_is_backwards_compatible_without_new_fields() -> None:
    from app.schemas.invest_screener import ScreenerFreshness

    payload = ScreenerFreshness(
        fetchedAt="2026-05-20T00:10:00+00:00",
        asOfLabel="2026.05.20 09:10 기준",
        relativeLabel="방금 갱신",
        cacheHit=False,
        source="live",
        dataState="fresh",
    )
    assert payload.primary is None
    assert payload.dependencies == []
    assert payload.overallState is None


@pytest.mark.unit
def test_freshness_primary_accepts_degradation_reason_and_coverage_label():
    from app.schemas.invest_screener import ScreenerFreshnessPrimary

    primary = ScreenerFreshnessPrimary(
        kind="screener_snapshot",
        asOfLabel="2026.06.03 15:30 기준",
        dataState="stale",
        degradationReason="coverage_below_floor",
        coverageLabel="20 / 3,800 (0.5%)",
    )
    assert primary.degradationReason == "coverage_below_floor"
    assert primary.coverageLabel == "20 / 3,800 (0.5%)"
    # defaults remain optional/None
    bare = ScreenerFreshnessPrimary(kind="live", asOfLabel="x", dataState="missing")
    assert bare.degradationReason is None
    assert bare.coverageLabel is None


@pytest.mark.unit
def test_result_row_accepts_market_cap_source():
    from app.schemas.invest_screener import ScreenerResultRow

    row = ScreenerResultRow(
        rank=1,
        symbol="005930",
        market="kr",
        name="삼성전자",
        priceLabel="70,000원",
        changePctLabel="+1.0%",
        changeAmountLabel="+700원",
        changeDirection="up",
        category="-",
        marketCapLabel="418조원",
        volumeLabel="1,000,000",
        analystLabel="-",
        metricValueLabel="-",
        marketCapSource="fallback",
    )
    assert row.marketCapSource == "fallback"
    bare = ScreenerResultRow(
        rank=1,
        symbol="x",
        market="kr",
        name="x",
        priceLabel="-",
        changePctLabel="-",
        changeAmountLabel="-",
        changeDirection="flat",
        category="-",
        marketCapLabel="-",
        volumeLabel="-",
        analystLabel="-",
        metricValueLabel="-",
    )
    assert bare.marketCapSource is None


@pytest.mark.unit
def test_result_row_accepts_analysis_context():
    from app.schemas.invest_screener import (
        ScreenerAnalysisConsensus,
        ScreenerAnalysisContext,
        ScreenerResultRow,
    )

    row = ScreenerResultRow(
        rank=1,
        symbol="005930",
        market="kr",
        name="삼성전자",
        priceLabel="70,000원",
        changePctLabel="+1.0%",
        changeAmountLabel="+700원",
        changeDirection="up",
        category="반도체",
        marketCapLabel="418조원",
        volumeLabel="1,000,000",
        analystLabel="매수 2 / 보유 1 / 매도 0 · 목표 +12.3%",
        metricValueLabel="+5.0%",
        analysisContext=ScreenerAnalysisContext(
            consensus=ScreenerAnalysisConsensus(
                source="naver",
                buyCount=2,
                holdCount=1,
                sellCount=0,
                strongBuyCount=0,
                totalCount=3,
                avgTargetPrice=78500.0,
                medianTargetPrice=78000.0,
                minTargetPrice=76000.0,
                maxTargetPrice=81000.0,
                upsidePct=12.3,
                currentPrice=69900.0,
            ),
            rsi14=58.42,
            dataState="fresh",
            warnings=[],
        ),
    )

    assert row.analysisContext is not None
    assert row.analysisContext.consensus is not None
    assert row.analysisContext.consensus.buyCount == 2
    assert row.analysisContext.rsi14 == pytest.approx(58.42)
