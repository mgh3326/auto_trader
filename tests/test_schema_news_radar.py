# tests/test_schema_news_radar.py
from datetime import UTC, datetime

import pytest

from app.schemas.news_radar import (
    NewsRadarItem,
    NewsRadarReadiness,
    NewsRadarResponse,
    NewsRadarSection,
    NewsRadarSourceCoverage,
    NewsRadarSummary,
)


@pytest.mark.unit
def test_radar_response_round_trips() -> None:
    payload = {
        "market": "all",
        "as_of": datetime(2026, 5, 5, 0, 0, tzinfo=UTC).isoformat(),
        "readiness": {
            "status": "ready",
            "latest_scraped_at": None,
            "latest_published_at": None,
            "recent_6h_count": 12,
            "recent_24h_count": 80,
            "source_count": 6,
            "stale": False,
            "warnings": [],
            "max_age_minutes": 180,
        },
        "summary": {
            "high_risk_count": 3,
            "total_count": 50,
            "included_in_briefing_count": 8,
            "excluded_but_collected_count": 42,
        },
        "sections": [
            {
                "section_id": "geopolitical_oil",
                "title": "Geopolitical / Oil shock",
                "severity": "high",
                "items": [],
            }
        ],
        "items": [],
        "excluded_items": [],
        "source_coverage": [],
    }

    response = NewsRadarResponse.model_validate(payload)
    assert response.market == "all"
    assert response.summary.high_risk_count == 3
    assert response.sections[0].severity == "high"


@pytest.mark.unit
def test_news_radar_item_marks_included_in_briefing() -> None:
    item = NewsRadarItem(
        id="42",
        title="UAE airstrike",
        source="Reuters",
        feed_source="rss_reuters",
        url="https://example.test/a",
        published_at=None,
        market="us",
        risk_category="geopolitical_oil",
        severity="high",
        themes=["oil", "defense"],
        symbols=["XOM"],
        included_in_briefing=False,
        briefing_reason="filtered_out_low_rank_or_not_selected",
        briefing_score=12,
        snippet="...",
        matched_terms=["uae", "airstrike"],
    )
    assert item.included_in_briefing is False
    assert item.severity == "high"


@pytest.mark.unit
def test_readiness_defaults_warnings_to_empty_list() -> None:
    readiness = NewsRadarReadiness(
        status="unavailable",
        latest_scraped_at=None,
        latest_published_at=None,
        recent_6h_count=0,
        recent_24h_count=0,
        source_count=0,
        stale=True,
        max_age_minutes=180,
    )
    assert readiness.warnings == []


@pytest.mark.unit
def test_section_and_summary_defaults() -> None:
    summary = NewsRadarSummary(
        high_risk_count=0,
        total_count=0,
        included_in_briefing_count=0,
        excluded_but_collected_count=0,
    )
    assert summary.total_count == 0
    section = NewsRadarSection(
        section_id="macro_policy",
        title="Macro / Policy",
        severity="medium",
        items=[],
    )
    assert section.items == []
    coverage = NewsRadarSourceCoverage(
        feed_source="rss_reuters",
        recent_6h=2,
        recent_24h=10,
        latest_published_at=None,
        latest_scraped_at=None,
        status="ready",
    )
    assert coverage.feed_source == "rss_reuters"
