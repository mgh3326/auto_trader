# tests/test_news_issue_clustering.py
"""Unit tests for the deterministic news issue clustering MVP (ROB-130)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.timezone import now_kst_naive
from app.services import news_issue_clustering_service as clustering


def _mk(*, id: int, title: str, source: str, summary: str = "",
        published_minutes_ago: int = 30, keywords: list[str] | None = None,
        market: str = "us"):
    return SimpleNamespace(
        id=id,
        title=title,
        summary=summary,
        source=source,
        feed_source=f"rss_{source}",
        url=f"https://example.com/{id}",
        market=market,
        keywords=keywords or [],
        article_published_at=now_kst_naive() - timedelta(minutes=published_minutes_ago),
        stock_symbol=None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clusters_articles_sharing_amazon_entity(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon raises guidance on AWS demand", source="cnbc"),
        _mk(id=2, title="AWS growth boosts Amazon outlook", source="bloomberg"),
        _mk(id=3, title="Apple reports record iPhone sales", source="reuters"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert result.market == "us"
    titles = [iss.issue_title for iss in result.items]
    assert any("Amazon" in t or "AMZN" in t for t in titles)

    amzn_issue = next(iss for iss in result.items if any(
        rs.symbol == "AMZN" for rs in iss.related_symbols))
    assert amzn_issue.article_count == 2
    assert amzn_issue.source_count == 2
    article_ids = {a.id for a in amzn_issue.articles}
    assert article_ids == {1, 2}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rank_orders_by_score_desc(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon up", source="cnbc", published_minutes_ago=10),
        _mk(id=2, title="Amazon AWS", source="bloomberg", published_minutes_ago=15),
        _mk(id=3, title="Amazon retail", source="reuters", published_minutes_ago=20),
        _mk(id=4, title="Tesla recall report", source="cnbc", published_minutes_ago=180),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert result.items[0].rank == 1
    # Amazon issue (3 articles, 3 sources, fresh) must outrank Tesla (1 article)
    assert any(rs.symbol == "AMZN" for rs in result.items[0].related_symbols)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_empty_when_no_articles(monkeypatch):
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=[])
    )
    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert result.items == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_id_is_stable_for_same_input(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon raises guidance", source="cnbc"),
        _mk(id=2, title="AWS demand boosts Amazon", source="bloomberg"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    first = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    second = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert [iss.id for iss in first.items] == [iss.id for iss in second.items]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_clustering_groups_005930(monkeypatch):
    rows = [
        _mk(id=11, title="삼성전자 1분기 실적 호조", source="mk", market="kr"),
        _mk(id=12, title="삼전 어닝 서프라이즈", source="hankyung", market="kr"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(market="kr", window_hours=24, limit=10)
    assert any(
        any(rs.symbol == "005930" for rs in iss.related_symbols)
        for iss in result.items
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signal_scores_are_in_unit_interval(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon up", source="cnbc"),
        _mk(id=2, title="Amazon up", source="bloomberg"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )
    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    for iss in result.items:
        assert 0.0 <= iss.signals.recency_score <= 1.0
        assert 0.0 <= iss.signals.source_diversity_score <= 1.0
        assert 0.0 <= iss.signals.mention_score <= 1.0
