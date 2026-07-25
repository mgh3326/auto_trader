"""ROB-628 AREA A2: get_market_issues hard response-size cap.

Oversized responses are trimmed (trailing issues / member articles dropped)
and explicitly flagged via truncated_for_size + degraded_reason — never a
silent drop, never fabricated filler.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.timezone import now_kst_naive
from app.schemas.news_issues import (
    IssueQualityGate,
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
    MarketIssuesResponse,
)
from app.services.news_text import NEWS_RESPONSE_MAX_CHARS


def _big_article(article_id: int) -> MarketIssueArticle:
    return MarketIssueArticle(
        id=article_id,
        title=f"Issue article {article_id} headline that is reasonably descriptive",
        url=f"https://example.com/{article_id}",
        source="cnbc",
        feed_source="rss_cnbc",
        published_at=now_kst_naive(),
        summary="x" * 1500,  # large full-detail body to blow past the cap
        matched_terms=["alpha", "beta"],
    )


def _big_issue(rank: int) -> MarketIssue:
    return MarketIssue(
        id=f"{rank:016d}",
        market="us",
        rank=rank,
        issue_title=f"Issue {rank}",
        subtitle="subtitle",
        direction="neutral",
        source_count=2,
        article_count=3,
        updated_at=now_kst_naive(),
        summary=None,
        related_symbols=[],
        related_sectors=[],
        articles=[_big_article(rank * 100 + k) for k in range(3)],
        signals=IssueSignals(
            recency_score=0.5, source_diversity_score=0.5, mention_score=0.5
        ),
    )


def _response(n_issues: int) -> MarketIssuesResponse:
    return MarketIssuesResponse(
        market="us",
        as_of=now_kst_naive(),
        window_hours=24,
        items=[_big_issue(r) for r in range(1, n_issues + 1)],
        status="ok",
        degraded_reason=None,
        quality_gate=IssueQualityGate(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_response_trims_issues_and_flags():
    from app.mcp_server.tooling import news_handlers

    big = _response(n_issues=12)
    raw_len = len(json.dumps(big.model_dump(mode="json"), ensure_ascii=False))
    assert raw_len > NEWS_RESPONSE_MAX_CHARS  # precondition: trimmer must engage

    with patch(
        "app.services.news_issue_clustering_service.build_market_issues",
        new=AsyncMock(return_value=big),
    ):
        result = await news_handlers._get_market_issues_impl(
            market="us", window_hours=24, limit=20, detail="full"
        )

    assert result["truncated_for_size"] is True
    assert result["degraded_reason"]
    assert "size cap" in result["degraded_reason"]
    # Hard cap honoured.
    assert len(json.dumps(result, ensure_ascii=False)) <= NEWS_RESPONSE_MAX_CHARS
    # Issues were genuinely trimmed (no fabrication), but never emptied.
    assert 1 <= len(result["items"]) < 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_within_cap_response_untouched():
    from app.mcp_server.tooling import news_handlers

    small = _response(n_issues=1)
    assert (
        len(json.dumps(small.model_dump(mode="json"), ensure_ascii=False))
        <= NEWS_RESPONSE_MAX_CHARS
    )

    with patch(
        "app.services.news_issue_clustering_service.build_market_issues",
        new=AsyncMock(return_value=small),
    ):
        result = await news_handlers._get_market_issues_impl(market="us")

    assert result["truncated_for_size"] is False
    assert result["degraded_reason"] is None
    assert len(result["items"]) == 1
    assert len(result["items"][0]["articles"]) == 3  # member articles intact


@pytest.mark.unit
def test_size_cap_helper_is_deterministic_and_counts():
    from app.mcp_server.tooling.news_handlers import _enforce_market_issues_size_cap

    payload = _response(n_issues=12).model_dump(mode="json")
    original_issues = len(payload["items"])
    original_articles = sum(len(it["articles"]) for it in payload["items"])
    capped = _enforce_market_issues_size_cap(payload)

    assert capped["truncated_for_size"] is True
    assert len(json.dumps(capped, ensure_ascii=False)) <= NEWS_RESPONSE_MAX_CHARS

    kept_issues = len(capped["items"])
    kept_articles = sum(len(it["articles"]) for it in capped["items"])
    dropped_issues = original_issues - kept_issues
    dropped_articles = original_articles - kept_articles
    # Real trimming happened (no fabrication) — both issues and member articles.
    assert kept_issues < original_issues
    assert dropped_issues > 0
    assert dropped_articles > 0
    # The reason reports the EXACT counts (not just the words), proving the
    # dropped_issues/dropped_articles arithmetic is accurate.
    assert (
        f"trimmed {dropped_issues} issue(s) and {dropped_articles} member article(s)"
        in capped["degraded_reason"]
    )


@pytest.mark.unit
def test_size_cap_preserves_existing_degraded_reason():
    from app.mcp_server.tooling.news_handlers import _enforce_market_issues_size_cap

    payload = _response(n_issues=12).model_dump(mode="json")
    payload["degraded_reason"] = "preexisting note"
    capped = _enforce_market_issues_size_cap(payload)
    assert capped["degraded_reason"].startswith("preexisting note; ")
