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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_judgment_transitions_and_is_idempotent(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = _unique_url("j1")
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, symbol, [_item(url, f"{symbol} 신규 투자")]
    )
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance)
            .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
            .where(NewsArticle.url == url, SymbolNewsRelevance.symbol == symbol)
        )
    ).scalar_one()

    status = await symbol_news_store.apply_judgment(
        db_session,
        article_id=link.article_id,
        market="kr",
        symbol=symbol,
        relationship="direct",
        relevance="high",
        price_relevance="catalyst",
        score=0.9,
        reason="직접 관련",
        judged_by="hermes",
    )
    assert status == "confirmed"

    # 재판정(overwrite) — unrelated → excluded
    status2 = await symbol_news_store.apply_judgment(
        db_session,
        article_id=link.article_id,
        market="kr",
        symbol=symbol,
        relationship="unrelated",
        relevance="low",
        price_relevance="none",
        score=0.2,
        reason="재검토 결과 무관",
        judged_by="hermes",
    )
    assert status2 == "excluded"
    await db_session.refresh(link)
    assert link.status == "excluded"
    assert link.judged_at is not None
    assert link.judged_by == "hermes"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_judgment_missing_link_returns_none(db_session) -> None:
    status = await symbol_news_store.apply_judgment(
        db_session,
        article_id=999999999,
        market="kr",
        symbol="000000",
        relationship="direct",
        relevance="high",
        price_relevance="none",
        score=None,
        reason="x",
        judged_by="hermes",
    )
    assert status is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_pending_returns_article_fields_and_hints(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = _unique_url("p1")
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, symbol, [_item(url, f"{symbol} D2SF 펀딩")]
    )
    rows = await symbol_news_store.list_pending(
        db_session, "kr", limit=50, symbol=symbol
    )
    assert rows
    row = rows[0]
    assert row["url"] == url
    assert row["title"] == f"{symbol} D2SF 펀딩"
    assert row["hints"] is not None
    assert isinstance(row["article_id"], int)
    assert row["symbol"] == symbol


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_pending_omits_judged_links(db_session) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = _unique_url("p2")
    await symbol_news_store.upsert_kr_feed_articles(
        db_session, symbol, [_item(url, f"{symbol} 판정 완료 기사")]
    )
    rows = await symbol_news_store.list_pending(
        db_session, "kr", limit=50, symbol=symbol
    )
    await symbol_news_store.apply_judgment(
        db_session,
        article_id=rows[0]["article_id"],
        market="kr",
        symbol=symbol,
        relationship="direct",
        relevance="high",
        price_relevance="background",
        score=None,
        reason="r",
        judged_by="hermes",
    )
    assert (
        await symbol_news_store.list_pending(db_session, "kr", limit=50, symbol=symbol)
        == []
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_returns_new_pending_link_count(db_session) -> None:
    """ROB-506: 반환값 = 신규 생성된 pending link 수 (enqueue 트리거 근거)."""
    symbol = f"S-{uuid.uuid4()}"[:20]
    url1 = _unique_url("c1")
    url2 = _unique_url("c2")
    items = [_item(url1, f"{symbol} 신규 투자"), _item(url2, f"{symbol} 실적 발표")]

    first = await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, items)
    assert first == 2

    # 동일 윈도우 재호출 — 신규 link 없음
    second = await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, items)
    assert second == 0

    # 빈 입력 — 0
    assert await symbol_news_store.upsert_kr_feed_articles(db_session, symbol, []) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_feed_articles_us_market_and_summary(db_session) -> None:
    symbol = f"U-{uuid.uuid4()}"[:20]
    url = _unique_url("us1")
    items = [
        FeedArticleInput(
            url=url,
            title="AAPL beats estimates",
            source="Reuters",
            published_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
            summary="Apple Q2 revenue beat.",
        )
    ]

    created = await symbol_news_store.upsert_feed_articles(
        db_session,
        "us",
        symbol,
        items,
        feed_source=symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE,
    )
    assert created == 1

    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert article.market == "us"
    assert article.summary == "Apple Q2 revenue beat."
    assert article.feed_source == "finnhub_company_news"
    first_fetched_at = article.scraped_at
    assert first_fetched_at is not None

    link = (
        await db_session.execute(
            select(SymbolNewsRelevance).where(
                SymbolNewsRelevance.article_id == article.id,
                SymbolNewsRelevance.market == "us",
                SymbolNewsRelevance.symbol == symbol,
            )
        )
    ).scalar_one()
    assert link.status == "pending"
    assert link.feed_source == "finnhub_company_news"

    # 멱등: 재호출 시 신규 링크 0
    again = await symbol_news_store.upsert_feed_articles(
        db_session,
        "us",
        symbol,
        items,
        feed_source=symbol_news_store.FINNHUB_COMPANY_FEED_SOURCE,
    )
    assert again == 0

    # URL conflict no-op은 원천 획득 시각을 덮어쓰지 않고, canonical loader가
    # production NewsArticle column에서 그 시각을 그대로 복원한다.
    stored, _ = await symbol_news_store.load_symbol_news(db_session, symbol, "us", 10)
    assert stored[0].summary == "Apple Q2 revenue beat."
    assert stored[0].fetched_at == first_fetched_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_feed_articles_crypto_shared_article_two_symbols(
    db_session,
) -> None:
    """crypto는 general 피드 — 같은 기사가 심볼별 링크로 공유된다."""
    url = _unique_url("cr1")
    sym_a = f"KRW-BTC-{uuid.uuid4()}"[:30]
    sym_b = f"KRW-ETH-{uuid.uuid4()}"[:30]
    items = [
        FeedArticleInput(
            url=url, title="Crypto rally", source="CoinDesk", published_at=None
        )
    ]

    a = await symbol_news_store.upsert_feed_articles(
        db_session,
        "crypto",
        sym_a,
        items,
        feed_source=symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE,
    )
    b = await symbol_news_store.upsert_feed_articles(
        db_session,
        "crypto",
        sym_b,
        items,
        feed_source=symbol_news_store.FINNHUB_GENERAL_FEED_SOURCE,
    )
    assert (a, b) == (1, 1)

    count = (
        (await db_session.execute(select(NewsArticle).where(NewsArticle.url == url)))
        .scalars()
        .all()
    )
    assert len(count) == 1  # 기사 1건, 링크 2건


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_kr_wrapper_unchanged(db_session) -> None:
    """기존 KR 래퍼는 동작 불변 (market='kr', naver feed_source)."""
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = _unique_url("krw1")
    created = await symbol_news_store.upsert_kr_feed_articles(
        db_session, symbol, [_item(url, f"{symbol} 투자")]
    )
    assert created == 1
    article = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert article.market == "kr"
    assert article.feed_source == "naver_item_news"
