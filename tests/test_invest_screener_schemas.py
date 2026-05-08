"""ROB-147: Pydantic schema contract tests for /invest/api/screener/*."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.invest_screener import (
    ScreenerFilterChip,
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
