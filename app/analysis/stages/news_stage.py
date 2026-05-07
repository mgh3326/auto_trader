import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.core.timezone import now_kst_naive
from app.schemas.research_pipeline import (
    NewsSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)
from app.services.llm_news_service import (
    bulk_create_news_articles,
    get_news_articles_with_fallback,
)
from app.services.research_news_service import NormalizedArticle, fetch_symbol_news

logger = logging.getLogger(__name__)

MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH = 3


def _market_from_instrument(instrument_type: str) -> str:
    if instrument_type == "equity_us":
        return "us"
    if instrument_type == "crypto":
        return "crypto"
    return "kr"


@dataclass
class _OnDemandArticlePayload:
    """Shape compatible with bulk_create_news_articles input contract."""

    url: str
    title: str
    content: str | None
    summary: str | None
    source: str | None
    author: str | None
    stock_symbol: str | None
    stock_name: str | None
    published_at: datetime | None
    market: str
    feed_source: str
    keywords: list[str] | None


@dataclass
class _SignalArticle:
    """Minimal article shape consumed by _compute_signals_from_articles."""

    title: str
    article_published_at: datetime | None
    keywords: list[str]


def _to_persist_payloads(
    articles: list[NormalizedArticle],
    *,
    symbol: str,
    stock_name: str | None,
    market: str,
) -> list[_OnDemandArticlePayload]:
    payloads: list[_OnDemandArticlePayload] = []
    for art in articles:
        payloads.append(
            _OnDemandArticlePayload(
                url=art.url,
                title=art.title,
                content=None,
                summary=art.summary,
                source=art.source,
                author=None,
                stock_symbol=symbol,
                stock_name=stock_name,
                published_at=art.published_at,
                market=market,
                feed_source=f"research_on_demand_{art.provider}",
                keywords=None,
            )
        )
    return payloads


def _to_signal_articles(articles: list[NormalizedArticle]) -> list[_SignalArticle]:
    return [
        _SignalArticle(
            title=art.title,
            article_published_at=art.published_at,
            keywords=[],
        )
        for art in articles
    ]


async def _fetch_recent_headlines(
    symbol: str,
    instrument_type: str,
    *,
    stock_name: str | None,
) -> dict[str, Any]:
    """Fetch recent headlines, augmenting DB with on-demand provider fetch
    when symbol-tagged news is below threshold."""
    market = _market_from_instrument(instrument_type)

    lookup = await get_news_articles_with_fallback(
        symbol=symbol, market=market, hours=24, limit=20
    )
    articles = lookup.articles

    if len(articles) < MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH:
        fetched = await fetch_symbol_news(symbol, instrument_type, limit=20)
        if fetched:
            payloads = _to_persist_payloads(
                fetched, symbol=symbol, stock_name=stock_name, market=market
            )
            try:
                await bulk_create_news_articles(payloads)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "news_stage: bulk_create_news_articles failed: symbol=%s err=%s",
                    symbol,
                    exc,
                )
            lookup = await get_news_articles_with_fallback(
                symbol=symbol, market=market, hours=24, limit=20
            )
            articles = lookup.articles
            if not articles:
                logger.info(
                    "news_stage: using fetched headlines as signal fallback: symbol=%s",
                    symbol,
                )
                return _compute_signals_from_articles(_to_signal_articles(fetched))

    return _compute_signals_from_articles(articles)


def _compute_signals_from_articles(articles: list[Any]) -> dict[str, Any]:
    """Pure logic to compute sentiment/signals from a list of articles."""
    if not articles:
        return {
            "headlines": [],
            "headline_count": 0,
            "sentiment_score": 0.0,
            "top_themes": [],
            "urgent_flags": [],
            "newest_age_minutes": 0,
        }

    sentiments = []
    themes = []
    newest_dt = None

    # V1 Rule-based sentiment keywords
    POS_KEYWORDS = {
        "상승",
        "호재",
        "급등",
        "매수",
        "수익",
        "성장",
        "실적발표",
        "흑자",
        "soaring",
        "positive",
        "bullish",
        "buy",
        "growth",
        "earnings",
        "beat",
        "outperform",
    }
    NEG_KEYWORDS = {
        "하락",
        "악재",
        "급락",
        "매도",
        "손실",
        "위기",
        "적자",
        "전망하치",
        "falling",
        "negative",
        "bearish",
        "sell",
        "loss",
        "crisis",
        "miss",
        "underperform",
    }

    for article in articles:
        # Freshness
        if article.article_published_at:
            if newest_dt is None or article.article_published_at > newest_dt:
                newest_dt = article.article_published_at

        # Sentiment scoring (v1: keyword-based)
        score = 0.0
        title_lower = article.title.lower()
        if any(kw in title_lower for kw in POS_KEYWORDS):
            score += 0.5
        if any(kw in title_lower for kw in NEG_KEYWORDS):
            score -= 0.5

        # Cap score
        score = max(-1.0, min(1.0, score))
        sentiments.append(score)

        # Themes from keywords
        if article.keywords:
            # article.keywords is list or JSONB
            if isinstance(article.keywords, list):
                themes.extend(article.keywords)

    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

    # Dedupe and limit themes
    unique_themes = []
    for t in themes:
        if t not in unique_themes:
            unique_themes.append(t)
    top_themes = unique_themes[:10]

    # Freshness calculation
    newest_age_minutes = 0
    if newest_dt:
        now = now_kst_naive()
        diff = now - newest_dt.replace(tzinfo=None)
        newest_age_minutes = max(0, int(diff.total_seconds() / 60))

    return {
        "headlines": [
            {"title": a.title, "published_at": a.article_published_at} for a in articles
        ],
        "headline_count": len(articles),
        "sentiment_score": round(avg_sentiment, 2),
        "top_themes": top_themes,
        "urgent_flags": [],
        "newest_age_minutes": newest_age_minutes,
    }


class NewsStageAnalyzer(BaseStageAnalyzer):
    stage_type = "news"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        try:
            raw = await _fetch_recent_headlines(
                ctx.symbol,
                ctx.instrument_type,
                stock_name=ctx.symbol_name,
            )
        except Exception as exc:
            logger.error(f"News analysis failed for {ctx.symbol}: {exc}")
            return StageOutput(
                stage_type=self.stage_type,
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                signals=NewsSignals(
                    headline_count=0,
                    sentiment_score=0.0,
                    top_themes=[],
                    urgent_flags=[],
                ),
                snapshot_at=datetime.now(UTC),
            )

        signals = NewsSignals(
            headline_count=raw["headline_count"],
            sentiment_score=raw["sentiment_score"],
            top_themes=raw["top_themes"],
            urgent_flags=raw["urgent_flags"],
        )

        # Verdict mapping rule:
        # BULL: sentiment_score > 0.15 and headline_count > 0
        # BEAR: sentiment_score < -0.15 and headline_count > 0
        # NEUTRAL: otherwise
        verdict = StageVerdict.NEUTRAL
        if signals.headline_count > 0:
            if signals.sentiment_score > 0.15:
                verdict = StageVerdict.BULL
            elif signals.sentiment_score < -0.15:
                verdict = StageVerdict.BEAR

        return StageOutput(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=65,  # Moderate confidence for news stage
            signals=signals,
            snapshot_at=datetime.now(UTC),
            source_freshness=SourceFreshness(
                newest_age_minutes=raw["newest_age_minutes"],
                oldest_age_minutes=0,
                source_count=1,
            ),
        )
