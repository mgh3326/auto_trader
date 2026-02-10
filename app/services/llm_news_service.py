import json
import time
from datetime import UTC, datetime

from google import genai
from google.genai import types
from google.genai.types import HttpOptions
from sqlalchemy import func, select

from app.analysis.news_prompt import build_news_analysis_prompt
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.model_rate_limiter import ModelRateLimiter
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
                            parsed = json.loads(resp.text)
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
    content: str,
    source: str | None = None,
    author: str | None = None,
    stock_symbol: str | None = None,
    stock_name: str | None = None,
    published_at: datetime | None = None,
) -> NewsArticle:
    article = NewsArticle(
        url=url,
        title=title,
        article_content=content,
        source=source,
        author=author,
        stock_symbol=stock_symbol,
        stock_name=stock_name,
        article_published_at=published_at,
        scraped_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    async with AsyncSessionLocal() as db:
        db.add(article)
        await db.commit()
        await db.refresh(article)

    return article


async def get_news_articles(
    stock_symbol: str | None = None,
    sentiment: str | None = None,
    source: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[NewsArticle], int]:
    """
    Query news articles with optional filters.

    Returns (list_of_articles, total_count).
    """
    async with AsyncSessionLocal() as db:
        query = select(NewsArticle)

        if stock_symbol:
            query = query.where(NewsArticle.stock_symbol == stock_symbol)
        if source:
            query = query.where(NewsArticle.source == source)

        query = query.order_by(NewsArticle.article_published_at.desc().nulls_last())

        total_query = select(func.count()).select_from(NewsArticle)
        if stock_symbol:
            total_query = total_query.where(NewsArticle.stock_symbol == stock_symbol)
        if source:
            total_query = total_query.where(NewsArticle.source == source)

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
