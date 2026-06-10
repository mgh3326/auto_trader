"""symbol_news_store persistence seam (ROB-491 PR1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services import symbol_news_store
from app.services.symbol_news_store import FeedArticleInput


def _item(url: str, title: str, published: datetime | None = None) -> FeedArticleInput:
    return FeedArticleInput(
        url=url,
        title=title,
        source="매일경제",
        published_at=published or datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
    )


def _unique_url(prefix: str) -> str:
    return f"https://x/rob491-{prefix}-{uuid.uuid4()}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_is_idempotent_set_difference(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    url1 = _unique_url("a1")
    url2 = _unique_url("a2")
    url3 = _unique_url("a3")

    items = [
        _item(url1, f"{symbol} D2SF 투자"),
        _item(url2, f"{symbol} 아이바오 출산"),
    ]
    await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, items)
    # 같은 윈도우 재호출(중복) + 신규 1건 — 순서 무관, 멱등
    await symbol_news_store.upsert_kr_feed_articles(
        db_session,
        symbol,
        [_item(url3, f"{symbol} AI 보안 투자"), *items],
    )

    urls = (
        (
            await db_session.execute(
                select(NewsArticle.url).where(NewsArticle.url.in_([url1, url2, url3]))
            )
        )
        .scalars()
        .all()
    )
    assert sorted(urls) == sorted([url1, url2, url3])
    links = (
        (
            await db_session.execute(
                select(SymbolNewsRelevance).where(
                    SymbolNewsRelevance.symbol == symbol,
                    SymbolNewsRelevance.market == "kr",
                )
            )
        )
        .scalars()
        .all()
    )
    by_status = {link.status for link in links}
    assert by_status == {"pending"}
    assert len(links) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_attaches_hints_for_alias_match(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = _unique_url("h1")
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, symbol, [_item(url, f"{symbol} 신사업 공개")]
    )
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance)
            .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
            .where(NewsArticle.url == url, SymbolNewsRelevance.symbol == symbol)
        )
    ).scalar_one()
    assert link.hints is not None
    assert symbol in link.hints["alias_match"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_load_excludes_only_excluded_and_counts(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    url1 = _unique_url("l1")
    url2 = _unique_url("l2")
    url3 = _unique_url("l3")
    items = [
        _item(url1, f"{symbol} 호실적", datetime(2026, 6, 10, 9, tzinfo=UTC)),
        _item(url2, f"{symbol} 출산", datetime(2026, 6, 10, 8, tzinfo=UTC)),
        _item(url3, f"{symbol} 브리핑", datetime(2026, 6, 10, 7, tzinfo=UTC)),
    ]
    await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, items)
    # l2를 excluded로 직접 마킹
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance)
            .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
            .where(NewsArticle.url == url2, SymbolNewsRelevance.symbol == symbol)
        )
    ).scalar_one()
    link.status = "excluded"
    await db_session.flush()

    stored, excluded_count = await symbol_news_store.load_symbol_news(
        db_session, symbol, "kr", limit=10
    )
    titles = [row.title for row in stored]
    assert titles == [
        f"{symbol} 호실적",
        f"{symbol} 브리핑",
    ]  # published_at desc, excluded 제외
    assert excluded_count == 1
    assert stored[0].relevance["status"] == "pending"
    assert stored[0].relevance["hints"] is not None


@pytest.mark.unit
def test_derive_status_rules() -> None:
    assert symbol_news_store.derive_status("unrelated", "high") == "excluded"
    assert symbol_news_store.derive_status("direct", "low") == "excluded"
    assert symbol_news_store.derive_status("direct", "high") == "confirmed"
    assert symbol_news_store.derive_status("incidental", "medium") == "confirmed"
