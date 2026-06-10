from __future__ import annotations

from typing import Any

import pytest

from app.services.invest_view_model.screener_analysis_enrichment import (
    build_analyst_label,
    build_rsi14_from_closes,
    normalize_consensus_payload,
)


@pytest.mark.unit
def test_build_rsi14_from_closes_returns_latest_rsi():
    closes = [
        100,
        101,
        102,
        101,
        103,
        104,
        103,
        105,
        106,
        107,
        106,
        108,
        109,
        110,
        111,
    ]

    assert build_rsi14_from_closes(closes) == pytest.approx(84.07, abs=0.01)


@pytest.mark.unit
def test_normalize_consensus_payload_maps_snake_to_camel():
    payload: dict[str, Any] = {
        "source": "naver",
        "consensus": {
            "buy_count": 2,
            "hold_count": 1,
            "sell_count": 0,
            "strong_buy_count": 0,
            "total_count": 3,
            "avg_target_price": 78500,
            "median_target_price": 78000,
            "min_target_price": 76000,
            "max_target_price": 81000,
            "upside_pct": 12.3,
            "current_price": 69900,
        },
    }

    consensus, warnings = normalize_consensus_payload(payload)

    assert warnings == []
    assert consensus is not None
    assert consensus.buyCount == 2
    assert consensus.avgTargetPrice == pytest.approx(78500)
    assert consensus.upsidePct == pytest.approx(12.3)


@pytest.mark.unit
def test_build_analyst_label_uses_counts_and_upside():
    payload: dict[str, Any] = {
        "source": "naver",
        "consensus": {
            "buy_count": 2,
            "hold_count": 1,
            "sell_count": 0,
            "total_count": 3,
            "upside_pct": 12.34,
        },
    }
    consensus, warnings = normalize_consensus_payload(payload)

    assert warnings == []
    assert build_analyst_label(consensus) == "매수 2 / 보유 1 / 매도 0 · 목표 +12.3%"


@pytest.mark.unit
def test_consensus_sanity_warning_when_bullish_target_below_current():
    payload: dict[str, Any] = {
        "source": "naver",
        "consensus": {
            "buy_count": 3,
            "hold_count": 0,
            "sell_count": 0,
            "total_count": 3,
            "avg_target_price": 90000,
            "current_price": 100000,
            "upside_pct": 15.0,
        },
    }

    consensus, warnings = normalize_consensus_payload(payload)

    assert consensus is not None
    assert "consensus_target_below_current_with_bullish_votes" in warnings
    assert "consensus_upside_mismatch" in warnings
    assert build_analyst_label(consensus, warnings=warnings) == "컨센 확인필요"
