import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst_naive, to_kst_naive
from app.models.news import NewsAnalysisResult, NewsArticle, NewsIngestionRun
from app.schemas.news import (
    NewsBulkIngestRequest,
    NewsBulkIngestResponse,
    NewsReadinessResponse,
    NewsSourceCoverage,
)

_CORE_FEED_SOURCES_BY_MARKET: dict[str, tuple[str, ...]] = {
    "us": (
        "rss_yahoo_finance_topstories",
        "rss_marketwatch_topstories",
        "rss_cnbc_us_markets",
        "rss_cnbc_earnings",
        "rss_cnbc_finance",
    ),
}


async def create_news_article(
    title: str,
    url: str,
    content: str | None = None,
    source: str | None = None,
    author: str | None = None,
    stock_symbol: str | None = None,
    stock_name: str | None = None,
    published_at: datetime | None = None,
    market: str = "kr",
    feed_source: str | None = None,
    keywords: list[str] | None = None,
    summary: str | None = None,
) -> NewsArticle:
    article = NewsArticle(
        url=url,
        title=title,
        article_content=content,
        summary=summary,
        source=source,
        author=author,
        stock_symbol=stock_symbol,
        stock_name=stock_name,
        article_published_at=to_kst_naive(published_at) if published_at else None,
        market=market,
        feed_source=feed_source,
        keywords=keywords,
        scraped_at=now_kst_naive(),
        created_at=now_kst_naive(),
    )

    async with AsyncSessionLocal() as db:
        db.add(article)
        await db.commit()
        await db.refresh(article)

    return article


async def bulk_create_news_articles(
    articles: list,
) -> tuple[int, int, list[str]]:
    """Insert crawled news rows and skip exact duplicate URLs.

    This intentionally does not do title/source/published_at fuzzy dedupe.
    URL-only dedupe is the current production rule for the briefing pipeline.
    """
    if not articles:
        return 0, 0, []

    urls = [a.url.strip() for a in articles]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(NewsArticle.url).where(NewsArticle.url.in_(urls))
        )
        existing_urls = set(result.scalars().all())

        inserted_count = 0
        skipped_urls = []

        for article_data in articles:
            url = article_data.url.strip()
            if url in existing_urls:
                skipped_urls.append(url)
                continue

            article = NewsArticle(
                url=url,
                title=article_data.title.strip(),
                article_content=article_data.content,
                summary=article_data.summary,
                source=article_data.source,
                author=article_data.author,
                stock_symbol=article_data.stock_symbol,
                stock_name=article_data.stock_name,
                article_published_at=to_kst_naive(article_data.published_at)
                if article_data.published_at
                else None,
                market=article_data.market,
                feed_source=article_data.feed_source,
                keywords=article_data.keywords,
                scraped_at=now_kst_naive(),
                created_at=now_kst_naive(),
            )
            db.add(article)
            existing_urls.add(url)
            inserted_count += 1

        if inserted_count > 0:
            await db.commit()

    return inserted_count, len(skipped_urls), skipped_urls


async def get_news_articles(
    market: str | None = None,
    stock_symbol: str | None = None,
    sentiment: str | None = None,
    source: str | None = None,
    limit: int = 10,
    offset: int = 0,
    hours: int | None = None,
    feed_source: str | None = None,
    keyword: str | None = None,
    has_analysis: bool | None = None,
) -> tuple[list[NewsArticle], int]:
    """Query news articles with optional filters.

    Returns (list_of_articles, total_count).
    """
    async with AsyncSessionLocal() as db:
        from sqlalchemy import exists

        query = select(NewsArticle).distinct()
        total_query = select(func.count(NewsArticle.id)).select_from(NewsArticle)

        conditions = []

        if market:
            conditions.append(NewsArticle.market == market)
        if stock_symbol:
            conditions.append(NewsArticle.stock_symbol == stock_symbol)
        if source:
            conditions.append(NewsArticle.source == source)
        if feed_source:
            conditions.append(NewsArticle.feed_source == feed_source)
        if hours is not None:
            cutoff = now_kst_naive() - timedelta(hours=hours)
            conditions.append(NewsArticle.article_published_at >= cutoff)
        if keyword:
            conditions.append(
                NewsArticle.keywords.op("@>")(cast(json.dumps([keyword]), JSONB))
            )
        if has_analysis is True:
            conditions.append(NewsArticle.is_analyzed.is_(True))
        elif has_analysis is False:
            conditions.append(NewsArticle.is_analyzed.is_(False))
        if sentiment:
            conditions.append(
                exists().where(
                    (NewsAnalysisResult.article_id == NewsArticle.id)
                    & (NewsAnalysisResult.sentiment == sentiment)
                )
            )

        for cond in conditions:
            query = query.where(cond)
            total_query = total_query.where(cond)

        query = query.order_by(NewsArticle.article_published_at.desc().nulls_last())

        result = await db.execute(query.offset(offset).limit(limit))
        articles = result.scalars().all()

        count_result = await db.execute(total_query)
        total = count_result.scalar_one()

    return articles, total


async def get_news_analysis(article_id: int) -> NewsAnalysisResult | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(NewsAnalysisResult)
            .where(NewsAnalysisResult.article_id == article_id)
            .order_by(NewsAnalysisResult.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()


def _news_readiness_payload(
    *,
    market: str,
    latest_run: NewsIngestionRun | None,
    latest_article_published_at: datetime | None,
    max_age_minutes: int,
    source_coverage: list[NewsSourceCoverage] | None = None,
) -> NewsReadinessResponse:
    warnings: list[str] = []
    latest_finished_at = latest_run.finished_at if latest_run else None
    if latest_finished_at is not None:
        latest_finished_at = to_kst_naive(latest_finished_at)
    if latest_article_published_at is not None:
        latest_article_published_at = to_kst_naive(latest_article_published_at)
    source_counts: dict[str, int] = latest_run.source_counts if latest_run else {}

    if latest_run is None:
        warnings.append("news_unavailable")
    elif latest_finished_at is None:
        warnings.append("news_run_unfinished")
    if latest_run is not None and not source_counts:
        warnings.append("news_sources_empty")

    freshness_anchor = latest_finished_at
    is_stale = True
    if freshness_anchor is not None and source_counts:
        is_stale = freshness_anchor < now_kst_naive() - timedelta(
            minutes=max_age_minutes
        )
    if is_stale:
        warnings.append("news_stale")

    is_ready = (
        latest_run is not None
        and latest_finished_at is not None
        and bool(source_counts)
        and not is_stale
    )

    return NewsReadinessResponse(
        market=market,
        is_ready=is_ready,
        is_stale=is_stale,
        latest_run_uuid=latest_run.run_uuid if latest_run else None,
        latest_status=latest_run.status if latest_run else None,
        latest_finished_at=latest_finished_at,
        latest_article_published_at=latest_article_published_at,
        source_counts=source_counts,
        source_coverage=source_coverage or [],
        warnings=list(dict.fromkeys(warnings)),
        max_age_minutes=max_age_minutes,
    )


def _article_values_from_ingestor_payload(article_data: Any) -> dict[str, Any]:
    return {
        "url": article_data.url.strip(),
        "title": article_data.title.strip(),
        "article_content": article_data.content,
        "summary": article_data.summary,
        "source": article_data.source,
        "author": article_data.author,
        "stock_symbol": article_data.stock_symbol,
        "stock_name": article_data.stock_name,
        "article_published_at": to_kst_naive(article_data.published_at)
        if article_data.published_at
        else None,
        "market": article_data.market,
        "feed_source": article_data.feed_source,
        "keywords": article_data.keywords,
        "scraped_at": now_kst_naive(),
        "created_at": now_kst_naive(),
    }


async def ingest_news_ingestor_bulk(
    request: NewsBulkIngestRequest,
) -> NewsBulkIngestResponse:
    """Persist a news-ingestor normalized payload through auto_trader boundary.

    Articles are deduped by the URL stored in auto_trader (canonical_url if supplied,
    otherwise url). No auto_trader DB direct insert from news-ingestor is used; this is
    the HTTP/API service boundary counterpart.
    """
    urls = [article.url.strip() for article in request.articles]

    async with AsyncSessionLocal() as db:
        existing_run_result = await db.execute(
            select(NewsIngestionRun).where(
                NewsIngestionRun.run_uuid == request.ingestion_run.run_uuid
            )
        )
        existing_run = existing_run_result.scalars().first()
        if existing_run is not None:
            return NewsBulkIngestResponse(
                success=True,
                run_uuid=existing_run.run_uuid,
                inserted_count=existing_run.inserted_count,
                skipped_count=existing_run.skipped_count,
                skipped_urls=[],
            )

        article_values = [
            _article_values_from_ingestor_payload(article_data)
            for article_data in request.articles
        ]
        article_stmt = (
            pg_insert(NewsArticle)
            .values(article_values)
            .on_conflict_do_nothing(index_elements=[NewsArticle.url])
            .returning(NewsArticle.url)
        )
        article_result = await db.execute(article_stmt)
        inserted_urls = set(article_result.scalars().all())

        consumed_inserted_urls: set[str] = set()
        skipped_urls: list[str] = []
        for url in urls:
            if url in inserted_urls and url not in consumed_inserted_urls:
                consumed_inserted_urls.add(url)
            else:
                skipped_urls.append(url)
        inserted_count = len(inserted_urls)

        run = request.ingestion_run
        run_values = {
            "run_uuid": run.run_uuid,
            "market": run.market,
            "feed_set": run.feed_set,
            "started_at": to_kst_naive(run.started_at) if run.started_at else None,
            "finished_at": to_kst_naive(run.finished_at) if run.finished_at else None,
            "status": run.status,
            "source_counts": dict(run.source_counts),
            "inserted_count": inserted_count,
            "skipped_count": len(skipped_urls),
            "error_message": run.error_message,
            "created_at": now_kst_naive(),
        }
        run_stmt = (
            pg_insert(NewsIngestionRun)
            .values(run_values)
            .on_conflict_do_nothing(index_elements=[NewsIngestionRun.run_uuid])
            .returning(NewsIngestionRun.run_uuid)
        )
        run_result = await db.execute(run_stmt)
        inserted_run_uuid = run_result.scalar_one_or_none()
        if inserted_run_uuid is None:
            existing_run_result = await db.execute(
                select(NewsIngestionRun).where(
                    NewsIngestionRun.run_uuid == request.ingestion_run.run_uuid
                )
            )
            existing_run = existing_run_result.scalars().first()
            await db.commit()
            if existing_run is not None:
                return NewsBulkIngestResponse(
                    success=True,
                    run_uuid=existing_run.run_uuid,
                    inserted_count=existing_run.inserted_count,
                    skipped_count=existing_run.skipped_count,
                    skipped_urls=[],
                )
        await db.commit()

    return NewsBulkIngestResponse(
        success=True,
        run_uuid=request.ingestion_run.run_uuid,
        inserted_count=inserted_count,
        skipped_count=len(skipped_urls),
        skipped_urls=skipped_urls,
    )


async def _build_source_coverage(
    session: AsyncSession,
    *,
    market: str,
    source_counts: dict[str, int],
) -> list[NewsSourceCoverage]:
    """Summarize per-feed storage freshness for readiness dashboards."""
    feed_sources = list(
        dict.fromkeys(
            [*source_counts.keys(), *_CORE_FEED_SOURCES_BY_MARKET.get(market, ())]
        )
    )
    if not feed_sources:
        return []
    cutoff_24h = now_kst_naive() - timedelta(hours=24)
    cutoff_6h = now_kst_naive() - timedelta(hours=6)
    result = await session.execute(
        select(
            NewsArticle.feed_source.label("feed_source"),
            func.count(NewsArticle.id).label("stored_total"),
            func.count(NewsArticle.article_published_at).label("published_at_count"),
            func.max(NewsArticle.article_published_at).label("latest_published_at"),
            func.max(NewsArticle.scraped_at).label("latest_scraped_at"),
            func.count(NewsArticle.id)
            .filter(NewsArticle.article_published_at >= cutoff_24h)
            .label("recent_24h"),
            func.count(NewsArticle.id)
            .filter(NewsArticle.article_published_at >= cutoff_6h)
            .label("recent_6h"),
        )
        .where(
            NewsArticle.market == market,
            NewsArticle.feed_source.in_(feed_sources),
        )
        .group_by(NewsArticle.feed_source)
    )
    rows = {row["feed_source"]: row for row in result.mappings().all()}

    coverage: list[NewsSourceCoverage] = []
    stale_cutoff = now_kst_naive() - timedelta(hours=24)
    for feed_source in feed_sources:
        expected_count = source_counts.get(feed_source, 0)
        row = rows.get(feed_source)
        warnings: list[str] = []
        if row is None:
            coverage.append(
                NewsSourceCoverage(
                    feed_source=feed_source,
                    expected_count=expected_count,
                    status="unavailable",
                    warnings=["source_articles_missing"],
                )
            )
            continue

        latest_published_at = row["latest_published_at"]
        latest_scraped_at = row["latest_scraped_at"]
        stored_total = int(row["stored_total"] or 0)
        published_at_count = int(row["published_at_count"] or 0)
        if published_at_count == 0:
            warnings.append("published_at_missing")
        if latest_published_at is None or latest_published_at < stale_cutoff:
            warnings.append("source_stale")
        status = "ready" if not warnings else "stale"
        coverage.append(
            NewsSourceCoverage(
                feed_source=feed_source,
                expected_count=expected_count,
                stored_total=stored_total,
                recent_24h=int(row["recent_24h"] or 0),
                recent_6h=int(row["recent_6h"] or 0),
                latest_published_at=latest_published_at,
                latest_scraped_at=latest_scraped_at,
                published_at_count=published_at_count,
                status=status,
                warnings=warnings,
            )
        )
    return coverage


async def get_latest_news_preview(
    *,
    db: AsyncSession,
    feed_sources: list[str],
    limit: int = 5,
) -> list:
    """Return the N most recent news articles for the given feed sources.

    Read-only, no LLM. The caller derives feed_sources from the latest
    ingestion run's source_counts.keys().
    """
    from app.schemas.preopen import NewsArticlePreview

    if not feed_sources or limit <= 0:
        return []

    stmt = (
        select(NewsArticle)
        .where(NewsArticle.feed_source.in_(feed_sources))
        .order_by(NewsArticle.article_published_at.desc().nulls_last())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        NewsArticlePreview(
            id=row.id,
            title=row.title,
            url=row.url,
            source=row.source,
            feed_source=row.feed_source,
            published_at=row.article_published_at,
            summary=row.summary,
        )
        for row in rows
    ]


async def get_news_readiness(
    *,
    market: str = "kr",
    max_age_minutes: int = 180,
    db: AsyncSession | None = None,
) -> NewsReadinessResponse:
    """Return latest news ingestion freshness for readiness/preopen checks."""

    async def _query(session: AsyncSession) -> NewsReadinessResponse:
        run_result = await session.execute(
            select(NewsIngestionRun)
            .where(
                NewsIngestionRun.market == market,
                NewsIngestionRun.status.in_(["success", "partial"]),
            )
            .order_by(NewsIngestionRun.finished_at.desc().nulls_last())
            .limit(1)
        )
        latest_run = run_result.scalars().first()

        article_conditions = [NewsArticle.feed_source.is_not(None)]
        if latest_run and latest_run.source_counts:
            article_conditions.append(
                NewsArticle.feed_source.in_(list(latest_run.source_counts.keys()))
            )
        article_result = await session.execute(
            select(func.max(NewsArticle.article_published_at)).where(
                *article_conditions
            )
        )
        latest_article_published_at = article_result.scalar_one_or_none()

        source_coverage = await _build_source_coverage(
            session,
            market=market,
            source_counts=latest_run.source_counts if latest_run else {},
        )

        return _news_readiness_payload(
            market=market,
            latest_run=latest_run,
            latest_article_published_at=latest_article_published_at,
            max_age_minutes=max_age_minutes,
            source_coverage=source_coverage,
        )

    if db is not None:
        return await _query(db)

    async with AsyncSessionLocal() as session:
        return await _query(session)
