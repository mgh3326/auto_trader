"""Schema tests for /invest/api/feed/research (ROB-179)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError


def test_tab_enum_values():
    from app.schemas.invest_feed_research import FeedResearchTab

    ta = TypeAdapter(FeedResearchTab)
    valid_tabs = ["top", "latest", "mine", "watchlist", "holdings", "kr", "us"]
    for tab in valid_tabs:
        ta.validate_python(tab)

    with pytest.raises(ValidationError):
        ta.validate_python("crypto")

    with pytest.raises(ValidationError):
        ta.validate_python("invalid")


def test_feed_research_item_alias_round_trip():
    from app.schemas.invest_feed_research import FeedResearchItem

    data = {
        "id": 1,
        "source": "kis_truefriend",
        "published_at_text": "2026.05.10",
        "published_at": "2026-05-10T08:00:00Z",
        "detail_url": "https://example.com/detail",
        "pdf_url": "https://example.com/report.pdf",
        "symbol_candidates": [{"symbol": "005930", "market": "kr", "source": "t"}],
        "attribution_publisher": "Korea Investment",
        "attribution_copyright_notice": "© Korea Investment",
        "market": "kr",
        "relation": "none",
    }
    item = FeedResearchItem.model_validate(data)
    dumped = item.model_dump(by_alias=True)

    assert "publishedAtText" in dumped
    assert "publishedAt" in dumped
    assert "detailUrl" in dumped
    assert "pdfUrl" in dumped
    assert "symbolCandidates" in dumped
    assert "attributionPublisher" in dumped
    assert "attributionCopyrightNotice" in dumped

    assert "published_at_text" not in dumped
    assert "published_at" not in dumped
    assert "detail_url" not in dumped
    assert "pdf_url" not in dumped
    assert "symbol_candidates" not in dumped
    assert "attribution_publisher" not in dumped
    assert "attribution_copyright_notice" not in dumped


def test_feed_research_item_rejects_body_fields():
    from app.schemas.invest_feed_research import FeedResearchItem

    base = {"id": 1, "source": "x", "market": "kr", "relation": "none"}
    banned_fields = [
        "pdf_body",
        "pdf_text",
        "extracted_text",
        "full_text",
        "article_content",
        "article_body",
        "raw_payload",
        "raw_payload_json",
        "dedup_key",
        "source_report_id",
        "ingestion_run_id",
        "pdf_sha256",
        "pdf_size_bytes",
        "pdf_page_count",
        "pdf_filename",
        "pdf_text_length",
        "attribution_full_text_exported",
        "attribution_pdf_body_exported",
        "raw_text_policy",
    ]
    for field in banned_fields:
        with pytest.raises(ValidationError, match="Extra inputs"):
            FeedResearchItem.model_validate({**base, field: "some_value"})


def test_response_envelope_shape():
    from app.schemas.invest_feed_research import (
        FeedResearchAppliedFilters,
        FeedResearchMeta,
        FeedResearchResponse,
    )

    # Missing fields should raise
    with pytest.raises(ValidationError):
        FeedResearchResponse.model_validate({})

    # Valid minimal response
    resp = FeedResearchResponse(
        tab="top",
        asOf=datetime.now(UTC),
        items=[],
        nextCursor=None,
        meta=FeedResearchMeta(
            limit=30,
            appliedFilters=FeedResearchAppliedFilters(),
        ),
    )
    assert resp.tab == "top"
    assert resp.items == []
    assert resp.nextCursor is None


def test_relation_enum():
    from pydantic import TypeAdapter

    from app.schemas.invest_feed_research import ResearchRelation

    ta = TypeAdapter(ResearchRelation)
    ta.validate_python("mine")
    ta.validate_python("watch")
    ta.validate_python("none")
    with pytest.raises(ValidationError):
        ta.validate_python("held")
    with pytest.raises(ValidationError):
        ta.validate_python("watchlist")
    with pytest.raises(ValidationError):
        ta.validate_python("both")


def test_market_enum():
    from pydantic import TypeAdapter

    from app.schemas.invest_feed_research import ResearchMarket

    ta = TypeAdapter(ResearchMarket)
    ta.validate_python("kr")
    ta.validate_python("us")
    ta.validate_python("crypto")
    with pytest.raises(ValidationError):
        ta.validate_python("forex")
    with pytest.raises(ValidationError):
        ta.validate_python("JP")
