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
from app.services.symbol_news_service import SymbolNewsArticle, fetch_symbol_news

logger = logging.getLogger(__name__)


def _market_from_instrument(instrument_type: str) -> str:
    if instrument_type == "equity_us":
        return "us"
    if instrument_type == "crypto":
        return "crypto"
    return "kr"


@dataclass
class _SignalArticle:
    """Minimal article shape consumed by _compute_signals_from_articles."""

    title: str
    article_published_at: datetime | None
    keywords: list[str]


def _to_signal_articles(articles: list[SymbolNewsArticle]) -> list[_SignalArticle]:
    # SymbolNewsArticle carries no keyword field; KR Naver / US+Crypto Finnhub
    # on-demand items provide none, so themes stay empty (already true for the
    # prior on-demand fallback path).
    return [
        _SignalArticle(
            title=a.title,
            article_published_at=a.published_at,
            keywords=[],
        )
        for a in articles
    ]


async def _fetch_recent_headlines(
    symbol: str,
    instrument_type: str,
) -> dict[str, Any]:
    """On-demand-first headlines via get_news/symbol_news_service (ROB-424).

    The broad ``news_articles`` DB is no longer read or written here, so the
    research-pipeline news verdict cannot be driven by the broad ingestor feed.
    The provider seam is fail-soft; ``status`` is carried in the returned dict so
    ``analyze`` can tell a provider error/unavailable from a genuine empty window.
    """
    market = _market_from_instrument(instrument_type)
    result = await fetch_symbol_news(symbol, market, limit=20)
    signals = _compute_signals_from_articles(_to_signal_articles(result.articles))
    signals["status"] = result.status
    return signals


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
            raw = await _fetch_recent_headlines(ctx.symbol, ctx.instrument_type)
        except Exception as exc:  # defensive — symbol_news_service is fail-soft
            logger.error(f"News analysis failed for {ctx.symbol}: {exc}")
            return self._unavailable()

        if raw.get("status") in ("error", "unavailable"):
            logger.info(
                "news_stage: provider status=%s for %s -> UNAVAILABLE",
                raw.get("status"),
                ctx.symbol,
            )
            return self._unavailable()

        signals = NewsSignals(
            headline_count=raw["headline_count"],
            sentiment_score=raw["sentiment_score"],
            top_themes=raw["top_themes"],
            urgent_flags=raw["urgent_flags"],
        )

        # Verdict mapping rule (status="ok"/"empty"):
        # BULL: sentiment_score > 0.15 and headline_count > 0
        # BEAR: sentiment_score < -0.15 and headline_count > 0
        # NEUTRAL: otherwise (includes empty window)
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

    def _unavailable(self) -> StageOutput:
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
