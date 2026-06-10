"""ROB-486 blast-radius 회귀 고정 — windowed consensus(None 목표가 + 메타데이터)가
다운스트림 소비자(스크리너 enrichment / 배치 요약 / 패널 스키마)를 깨지 않는다."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result
from app.mcp_server.tooling.fundamentals_sources_common import (
    _build_screen_enrichment_payload,
)
from app.schemas.invest_stock_detail_research_consensus import (
    StockDetailAnalystConsensus,
)

# 031330 실측 모양의 stale-only windowed consensus.
_STALE_ONLY_WINDOWED_CONSENSUS = {
    "buy_count": 0,
    "hold_count": 0,
    "sell_count": 0,
    "strong_buy_count": 0,
    "total_count": 0,
    "avg_target_price": None,
    "median_target_price": None,
    "min_target_price": None,
    "max_target_price": None,
    "upside_pct": None,
    "current_price": 15360,
    "rows_total": 2,
    "rows_used": 0,
    "rows_excluded_stale": 2,
    "rows_excluded_undated": 0,
    "newest_opinion_date": "2019-12-27",
    "window_months": 12,
}


@pytest.mark.unit
def test_screen_enrichment_payload_none_safe_with_stale_only_consensus():
    payload = _build_screen_enrichment_payload(
        sector="반도체", consensus=_STALE_ONLY_WINDOWED_CONSENSUS
    )

    assert payload["analyst_buy"] == 0
    assert payload["analyst_hold"] == 0
    assert payload["analyst_sell"] == 0
    assert payload["avg_target"] is None
    assert payload["upside_pct"] is None


@pytest.mark.unit
def test_batch_summary_passes_windowed_consensus_through():
    analysis = {
        "market_type": "equity_kr",
        "source": "naver",
        "quote": {"price": 15360},
        "opinions": {"consensus": _STALE_ONLY_WINDOWED_CONSENSUS},
        "recommendation": {"action": "hold", "confidence": "low"},
    }

    summary = _summarize_analysis_result("031330", analysis)

    assert summary["consensus"] == _STALE_ONLY_WINDOWED_CONSENSUS
    assert summary["consensus"]["rows_used"] == 0
    assert summary["recommendation"]["action"] == "hold"


@pytest.mark.unit
def test_stock_detail_consensus_schema_accepts_null_targets():
    model = StockDetailAnalystConsensus(
        source="naver",
        buyCount=0,
        holdCount=0,
        sellCount=0,
        strongBuyCount=0,
        totalCount=0,
        avgTargetPrice=None,
        medianTargetPrice=None,
        minTargetPrice=None,
        maxTargetPrice=None,
        upsidePct=None,
        currentPrice=15360.0,
    )
    assert model.avgTargetPrice is None
    assert model.upsidePct is None
