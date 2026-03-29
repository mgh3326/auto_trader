import json
import time
from datetime import datetime, timedelta

from google import genai
from google.genai import types
from google.genai.types import HttpOptions
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from app.analysis.news_prompt import build_news_analysis_prompt
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.model_rate_limiter import ModelRateLimiter
from app.core.timezone import now_kst_naive, to_kst_naive
from app.models.news import NewsAnalysisResult, NewsArticle, Sentiment

GEMINI_TIMEOUT = 3 * 60 * 1000


class NewsAnalyzer:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.get_random_key()
        self.client = genai.Client(
            api_key=self.api_key, http_options=HttpOptions(timeout=GEMINI_TIMEOUT)
        )
        self.rate_limiter = ModelRateLimiter()

    async def analyze_news(
        self,
        article_id: int,
        title: str,
        content: str,
        stock_symbol: str | None = None,
        stock_name: str | None = None,
        source: str | None = None,
    ) -> NewsAnalysisResult:
        start_time = time.time()

        prompt = build_news_analysis_prompt(
            title=title,
            content=content,
            stock_symbol=stock_symbol,
            stock_name=stock_name,
            source=source,
        )

        result, model_name = await self._generate_with_retry(prompt)
        processing_time_ms = int((time.time() - start_time) * 1000)

        if "error" in result:
            raise RuntimeError(f"LLM analysis failed: {result.get('error')}")

        analysis = NewsAnalysisResult(
            article_id=article_id,
            model_name=model_name,
            sentiment=Sentiment(result.get("sentiment", "neutral")),
            sentiment_score=result.get("sentiment_score"),
            summary=result.get("summary", ""),
            key_points=result.get("key_points", []),
            topics=result.get("topics"),
            price_impact=result.get("price_impact"),
            price_impact_score=result.get("price_impact_score"),
            confidence=result.get("confidence", 50),
            prompt=prompt,
            raw_response=json.dumps(result, ensure_ascii=False),
            processing_time_ms=processing_time_ms,
            created_at=now_kst_naive(),
        )

        async with AsyncSessionLocal() as db:
            db.add(analysis)
            await db.commit()
            await db.refresh(analysis)

        return analysis

    async def _generate_with_retry(
        self, prompt: str, max_retries: int = 3
    ) -> tuple[dict, str]:
        models_to_try = [
            "gemini-2.5-pro",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-preview-09-2025",
            "gemini-2.0-flash",
        ]

        for model in models_to_try:
            for attempt in range(max_retries):
                try:
                    resp = await self.client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                    )

                    if resp and resp.candidates:
                        candidate = resp.candidates[0]
                        finish_reason = getattr(candidate, "finish_reason", None)

                        if finish_reason in {
                            types.FinishReason.SAFETY,
                            types.FinishReason.RECITATION,
                        } or getattr(finish_reason, "value", None) in {
                            "SAFETY",
                            "RECITATION",
                        }:
                            print(f"{model} blocked: {finish_reason}")
                            break

                        if resp.text:
                            print(f"{model} success")
                            text = resp.text.strip()
                            if text.startswith("```json"):
                                text = text[7:]  # Remove ```json prefix
                            if text.startswith("```"):
                                text = text[3:]  # Remove ``` prefix
                            if text.endswith("```"):
                                text = text[:-3]  # Remove ``` suffix
                            text = text.strip()
                            parsed = json.loads(text)
                            return parsed, model

                except Exception as e:
                    print(f"{model} attempt {attempt + 1} failed: {e}")
                    await self._backoff(attempt)

        return {"error": "all_models_failed"}, "N/A"

    async def _backoff(self, attempt: int):
        import asyncio

        await asyncio.sleep(min(2**attempt, 10))

    async def close(self):
        await self.rate_limiter.close()


async def create_news_article(
    title: str,
    url: str,
    content: str | None = None,
    source: str | None = None,
    author: str | None = None,
    stock_symbol: str | None = None,
    stock_name: str | None = None,
    published_at: datetime | None = None,
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
