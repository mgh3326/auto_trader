"""SymbolNewsRelevance table contract (ROB-491)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


async def _make_article(db, url: str) -> NewsArticle:
    now = _utcnow()
    article = NewsArticle(
        url=url,
        title="t",
        market="kr",
        feed_source="naver_item_news",
        scraped_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(article)
    await db.flush()
    return article


@pytest.mark.integration
@pytest.mark.asyncio
async def test_link_roundtrip_defaults_pending(db_session) -> None:
    article = await _make_article(db_session, "https://x/rob491-roundtrip")
    now = _utcnow()
    link = SymbolNewsRelevance(
        article_id=article.id,
        market="kr",
        symbol="035420",
        feed_source="naver_item_news",
        first_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(link)
    await db_session.flush()
    await db_session.refresh(link)
    assert link.status == "pending"
    assert link.relationship is None
    assert link.judged_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_link_violates_unique(db_session) -> None:
    article = await _make_article(db_session, "https://x/rob491-dup")
    now = _utcnow()
    for _ in range(2):
        db_session.add(
            SymbolNewsRelevance(
                article_id=article.id,
                market="kr",
                symbol="035420",
                feed_source="naver_item_news",
                first_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()
