import logging
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
from app.services.llm_news_service import get_news_articles

logger = logging.getLogger(__name__)


async def _fetch_recent_headlines(symbol: str, instrument_type: str) -> dict[str, Any]:
    """Fetch recent headlines and compute basic sentiment/themes."""
    # Map instrument_type to market
    market = "kr"
    if instrument_type == "equity_us":
        market = "us"
    elif instrument_type == "crypto":
        market = "crypto"

    # Fetch articles from last 24 hours
    articles, total = await get_news_articles(
        stock_symbol=symbol, market=market, hours=24, limit=20
    )

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
            raw = await _fetch_recent_headlines(ctx.symbol, ctx.instrument_type)
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
