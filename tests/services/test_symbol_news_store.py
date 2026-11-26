"""symbol_news_store persistence seam (ROB-491 PR1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.sql.dml import Update

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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_store_keeps_naver_provider_url_as_article_key() -> None:
    provider_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=101"
    inserted_urls: list[str] = []

    class Result:
        rowcount = 1

        def __init__(self, rows=()):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class Session:
        async def execute(self, statement):
            if isinstance(statement, Insert):
                if statement.table.name == "news_articles":
                    params = statement.compile(dialect=postgresql.dialect()).params
                    url = next(
                        value for key, value in params.items() if key.startswith("url")
                    )
                    inserted_urls.append(url)
                    return Result([(123, True)])
                return Result()
            return Result([(123, provider_url)])

        async def commit(self):
            return None

    await symbol_news_store.upsert_feed_articles(
        Session(),
        "kr",
        "005930",
        [_item(provider_url, "삼성전자 기사")],
        feed_source=symbol_news_store.KR_FEED_SOURCE,
    )

    assert inserted_urls == [provider_url]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_reuses_existing_mobile_url_row_for_canonical_dedupe() -> None:
    existing_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=101"
    incoming_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=102"
    article_inserts = 0
    relevance_inserts: list[dict[str, object]] = []

    class Result:
        rowcount = 0

        def all(self):
            return [
                (
                    88,
                    existing_url,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ]

    class Session:
        async def execute(self, statement):
            nonlocal article_inserts
            if isinstance(statement, Insert):
                if statement.table.name == "news_articles":
                    article_inserts += 1
                    return Result()
                params = statement.compile(dialect=postgresql.dialect()).params
                relevance_inserts.append(params)
                return Result()
            return Result()

        async def commit(self):
            return None

    await symbol_news_store.upsert_kr_collector_articles(
        Session(),
        "005930",
        [_item(incoming_url, "삼성전자 기사")],
        feed_source="http_naver_stock_aggregate",
    )

    assert article_inserts == 0
    assert len(relevance_inserts) == 1
    assert 88 in relevance_inserts[0].values()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_enriches_legacy_provider_url_row_publication_fields() -> None:
    existing_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=101"
    incoming_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=102"
    updates: list[dict[str, object]] = []
    article_inserts = 0

    class Result:
        rowcount = 0

        def __init__(self, rows=()):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class Session:
        async def execute(self, statement):
            nonlocal article_inserts
            if isinstance(statement, Update):
                params = statement.compile(dialect=postgresql.dialect()).params
                updates.append(params)
                return Result()
            if isinstance(statement, Insert):
                if statement.table.name == "news_articles":
                    article_inserts += 1
                return Result()
            if len(statement.selected_columns) == 11:
                return Result(
                    [
                        (
                            88,
                            existing_url,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    ]
                )
            return Result([(88, existing_url)])

        async def commit(self):
            return None

    item = FeedArticleInput(
        url=incoming_url,
        title="삼성전자 기사",
        source="네이버뉴스",
        published_at=datetime(2026, 11, 25, 10, 0),
        published_at_raw="2026-11-25 10:00",
        published_precision="minute",
    )
    await symbol_news_store.upsert_kr_collector_articles(
        Session(),
        "005930",
        [item],
        feed_source="http_naver_stock_aggregate",
    )

    assert article_inserts == 0
    assert len(updates) == 1
    assert updates[0]["published_at_raw"] == "2026-11-25 10:00"
    assert updates[0]["published_precision"] == "minute"
    assert updates[0]["published_at"].astimezone(UTC).isoformat() == (
        "2026-11-25T01:00:00+00:00"
    )
    assert "first_received_at" not in updates[0]
    assert "url" not in updates[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_metadata_enrichment_keeps_publication_tuple_and_revision() -> None:
    existing_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=101"
    incoming_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=102"
    updates: list[dict[str, object]] = []

    class Result:
        rowcount = 0

        def all(self):
            return [
                (
                    88,
                    existing_url,
                    "2026-11-25 10:00:00",
                    datetime(2026, 11, 25, 1, 0, tzinfo=UTC),
                    "second",
                    "+09:00",
                    None,
                    None,
                    "unchanged title",
                    "unchanged source",
                    "unchanged summary",
                )
            ]

    class Session:
        async def execute(self, statement):
            if isinstance(statement, Update):
                updates.append(statement.compile(dialect=postgresql.dialect()).params)
                return Result()
            if isinstance(statement, Insert):
                return Result()
            if len(statement.selected_columns) == 11:
                return Result()
            return Result()

        async def commit(self):
            return None

    await symbol_news_store.upsert_kr_collector_articles(
        Session(),
        "005930",
        [
            FeedArticleInput(
                url=incoming_url,
                title="unchanged title",
                source="unchanged source",
                summary="unchanged summary",
                published_at=datetime(2026, 11, 25, 1, 0, tzinfo=UTC),
                published_at_raw="2026-11-25",
                published_precision="date",
                published_offset="-05:00",
                provider_article_id="provider-only-enrichment",
            )
        ],
        feed_source="http_naver_stock_aggregate",
    )

    assert len(updates) == 1
    assert updates[0]["provider_article_id"] == "provider-only-enrichment"
    assert "published_offset" not in updates[0]
    assert "published_at_raw" not in updates[0]
    assert "published_at" not in updates[0]
    assert "revised_at" not in updates[0]


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
async def test_collector_reingest_keeps_first_received_and_single_article(
    db_session,
) -> None:
    symbol = f"S-{uuid.uuid4()}"[:20]
    provider_url = "https://n.news.naver.com/mnews/article/001/0001234?sid=101"
    first = FeedArticleInput(
        url=provider_url,
        title="첫 수신",
        source="네이버뉴스",
        published_at=datetime(2026, 11, 25, 0, 0, tzinfo=UTC),
        published_at_raw="2026-11-25",
        published_precision="date",
    )
    await symbol_news_store.upsert_kr_collector_articles(
        db_session, symbol, [first], feed_source="http_naver_stock_aggregate"
    )
    stored = (
        await db_session.execute(
            select(NewsArticle).where(NewsArticle.url == provider_url)
        )
    ).scalar_one()
    received_at = stored.first_received_at
    article_id = stored.id
    assert (
        await symbol_news_store.apply_judgment(
            db_session,
            article_id=article_id,
            market="kr",
            symbol=symbol,
            relationship="unrelated",
            relevance="high",
            price_relevance="none",
            score=0.0,
            reason="not_about_symbol",
            judged_by="test",
        )
        == "excluded"
    )

    await symbol_news_store.upsert_kr_collector_articles(
        db_session,
        symbol,
        [
            FeedArticleInput(
                url="https://n.news.naver.com/mnews/article/001/0001234?sid=102",
                title="다시 수신",
                source="다른 출처",
                published_at=datetime(2026, 11, 25, 1, 0, tzinfo=UTC),
                published_at_raw="2026-11-25 10:00",
                published_precision="minute",
            )
        ],
        feed_source="http_naver_stock_aggregate",
    )
    rows = (
        (
            await db_session.execute(
                select(NewsArticle).where(
                    NewsArticle.url.startswith(provider_url.split("?", 1)[0])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].id == article_id
    assert rows[0].url == provider_url
    assert rows[0].first_received_at == received_at
    assert rows[0].revised_at is not None
    assert rows[0].published_at_raw == "2026-11-25 10:00"
    assert rows[0].published_at == datetime(2026, 11, 25, 1, 0, tzinfo=UTC)
    assert rows[0].published_precision == "minute"
    assert rows[0].published_offset == "+09:00"
    link = (
        await db_session.execute(
            select(SymbolNewsRelevance).where(
                SymbolNewsRelevance.article_id == article_id,
                SymbolNewsRelevance.market == "kr",
                SymbolNewsRelevance.symbol == symbol,
            )
        )
    ).scalar_one()
    assert link.status == "excluded"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_noncanonical_reingest_dedupes_and_preserves_existing_metadata(
    db_session,
) -> None:
    url = _unique_url("preserve")
    first_received = datetime(2026, 11, 20, 1, 2, tzinfo=UTC)
    published_at = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    db_session.add(
        NewsArticle(
            url=url,
            title="stored title",
            source="stored publisher",
            summary="stored summary",
            market="kr",
            feed_source="stored feed",
            scraped_at=first_received,
            created_at=first_received,
            first_received_at=first_received,
            published_at=published_at,
            published_at_raw="2026-11-20T09:00:00+09:00",
            published_offset="+09:00",
            published_precision="second",
            provider_article_id="provider-123",
            source_document_ids={"dart_id": ["20261120000001"]},
        )
    )
    await db_session.commit()

    less_complete = FeedArticleInput(
        url=url,
        title="stored title",
        source=None,
        summary=None,
        published_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
        published_at_raw="2026-11-20",
        published_precision="date",
    )
    await symbol_news_store._upsert_feed_articles(
        db_session,
        "kr",
        "005930",
        [less_complete, less_complete],
        feed_source="new feed",
    )

    stored = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert stored.first_received_at == first_received
    assert stored.revised_at is None
    assert stored.source == "stored publisher"
    assert stored.summary == "stored summary"
    assert stored.published_at == published_at
    assert stored.published_at_raw == "2026-11-20T09:00:00+09:00"
    assert stored.published_precision == "second"
    assert stored.provider_article_id == "provider-123"
    assert stored.source_document_ids == {"dart_id": ["20261120000001"]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canonical_reingest_keeps_nonempty_content_when_feed_omits_it(
    db_session,
) -> None:
    existing_url = "https://n.news.naver.com/mnews/article/001/0007654?sid=101"
    incoming_url = "https://n.news.naver.com/mnews/article/001/0007654?sid=102"
    first_received = datetime(2026, 11, 20, 1, 2, tzinfo=UTC)
    db_session.add(
        NewsArticle(
            url=existing_url,
            title="canonical title",
            source="stored publisher",
            summary="aggregate summary",
            market="kr",
            feed_source="http_naver_stock_aggregate",
            scraped_at=first_received,
            created_at=first_received,
            first_received_at=first_received,
            published_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
            published_at_raw="2026-11-20T09:00:00+09:00",
            published_precision="second",
        )
    )
    await db_session.commit()

    await symbol_news_store.upsert_kr_collector_articles(
        db_session,
        "005930",
        [
            FeedArticleInput(
                url=incoming_url,
                title="canonical title",
                source=None,
                summary=None,
                published_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
                published_at_raw="2026-11-20",
                published_precision="date",
                published_offset="-05:00",
            )
        ],
        feed_source="http_naver_stock_aggregate",
    )

    stored = (
        await db_session.execute(
            select(NewsArticle).where(NewsArticle.url == existing_url)
        )
    ).scalar_one()
    assert stored.first_received_at == first_received
    assert stored.revised_at is None
    assert stored.source == "stored publisher"
    assert stored.summary == "aggregate summary"
    assert stored.published_at_raw == "2026-11-20T09:00:00+09:00"
    assert stored.published_precision == "second"
    assert stored.published_offset == "+09:00"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canonical_metadata_enrichment_does_not_mark_article_revised(
    db_session,
) -> None:
    url = "https://n.news.naver.com/mnews/article/001/0009988?sid=101"
    first_received = datetime(2026, 11, 20, 1, 2, tzinfo=UTC)
    db_session.add(
        NewsArticle(
            url=url,
            title="unchanged title",
            source="unchanged source",
            summary="unchanged content",
            market="kr",
            feed_source="http_naver_stock_aggregate",
            scraped_at=first_received,
            created_at=first_received,
            first_received_at=first_received,
            published_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
            published_at_raw="2026-11-20T09:00:00+09:00",
            published_offset="+09:00",
            published_precision="second",
        )
    )
    await db_session.commit()

    await symbol_news_store.upsert_kr_collector_articles(
        db_session,
        "005930",
        [
            FeedArticleInput(
                url=url.replace("sid=101", "sid=102"),
                title="unchanged title",
                source="unchanged source",
                summary="unchanged content",
                published_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
                published_at_raw="2026-11-20T09:00:00+09:00",
                published_precision="second",
                published_offset="+09:00",
                provider_article_id="provider-enrichment",
            )
        ],
        feed_source="http_naver_stock_aggregate",
    )

    stored = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert stored.provider_article_id == "provider-enrichment"
    assert stored.revised_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canonical_publication_time_enrichment_marks_article_revised(
    db_session,
) -> None:
    url = "https://n.news.naver.com/mnews/article/001/0009989?sid=101"
    first_received = datetime(2026, 11, 20, 1, 2, tzinfo=UTC)
    db_session.add(
        NewsArticle(
            url=url,
            title="unchanged title",
            source="unchanged source",
            summary="unchanged content",
            market="kr",
            feed_source="http_naver_stock_aggregate",
            scraped_at=first_received,
            created_at=first_received,
            first_received_at=first_received,
            published_at=datetime(2026, 11, 19, 15, 0, tzinfo=UTC),
            published_at_raw="2026-11-20",
            published_precision="date",
        )
    )
    await db_session.commit()

    await symbol_news_store.upsert_kr_collector_articles(
        db_session,
        "005930",
        [
            FeedArticleInput(
                url=url.replace("sid=101", "sid=102"),
                title="unchanged title",
                source="unchanged source",
                summary="unchanged content",
                published_at=datetime(2026, 11, 19, 15, 30, tzinfo=UTC),
                published_at_raw="2026-11-20 09:30",
                published_precision="minute",
                published_offset="+09:00",
            )
        ],
        feed_source="http_naver_stock_aggregate",
    )

    stored = (
        await db_session.execute(select(NewsArticle).where(NewsArticle.url == url))
    ).scalar_one()
    assert stored.published_at_raw == "2026-11-20 09:30"
    assert stored.published_at == datetime(2026, 11, 19, 15, 30, tzinfo=UTC)
    assert stored.published_precision == "minute"
    assert stored.published_offset == "+09:00"
    assert stored.revised_at is not None


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
