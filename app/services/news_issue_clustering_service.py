# app/services/news_issue_clustering_service.py
"""Deterministic market-issue clustering MVP (ROB-130).

Read-only service. Groups recent articles by:
  1. Shared entity matches (alias dictionary)
  2. Title shingles (3-grams of normalized words) when no shared entity exists
Output is a stable, ranked list of `MarketIssue` objects.

Future LLM-powered impact summarization can replace `_pick_issue_title`/
`_pick_subtitle` without changing the contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst_naive
from app.models.news import NewsArticle
from app.schemas.news_issues import (
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
    MarketIssueRelatedSymbol,
    MarketIssuesResponse,
)
from app.services.news_entity_matcher import (
    SymbolMatch,
    match_symbols_for_article,
)

_DIR_POS_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:rise|raise|beat|surge|rally|up)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DIR_NEG_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:fall|drop|miss|plunge|down)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DIR_POS_KO = ("상승", "급등", "호재", "최고")
_DIR_NEG_KO = ("하락", "급락", "악재", "위기")
_WORD_RE = re.compile(r"[A-Za-z0-9가-힣]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "as", "at", "by", "with", "from", "이", "그", "저", "및", "는", "이는", "관련",
}


async def _load_recent_articles(
    *, market: str | None, window_hours: int, max_rows: int
) -> list[NewsArticle]:
    cutoff = now_kst_naive() - timedelta(hours=window_hours)
    async with AsyncSessionLocal() as db:
        stmt = (
            select(NewsArticle)
            .where(NewsArticle.article_published_at.is_not(None))
            .where(NewsArticle.article_published_at >= cutoff)
        )
        if market is not None and market != "all":
            stmt = stmt.where(NewsArticle.market == market)
        stmt = stmt.order_by(NewsArticle.article_published_at.desc()).limit(max_rows)
        result = await db.execute(stmt)
        return list(result.scalars().all())


def _normalize_words(text: str) -> list[str]:
    return [w for w in (m.lower() for m in _WORD_RE.findall(text or "")) if w not in _STOPWORDS]


def _shingles(words: list[str], n: int = 3) -> set[tuple[str, ...]]:
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


@dataclass
class _Cluster:
    article_ids: list[int]
    article_indexes: list[int]
    matches: list[SymbolMatch]
    cluster_key: str  # symbol-or-shingle-derived stable key


def _cluster_articles(
    articles: list[NewsArticle], market: str
) -> list[_Cluster]:
    """Two-pass clustering:
       1. Group by primary entity match (first symbol per article).
       2. Articles without entity → group by shared shingles (Jaccard >= 0.34).
    """
    by_symbol: dict[str, _Cluster] = {}
    leftover_indexes: list[int] = []
    leftover_shingles: list[set[tuple[str, ...]]] = []
    leftover_words: list[list[str]] = []

    for idx, art in enumerate(articles):
        matches = match_symbols_for_article(
            title=art.title,
            summary=getattr(art, "summary", None),
            keywords=getattr(art, "keywords", None) or [],
            market=market if market != "all" else None,
        )
        if matches:
            primary = matches[0]
            cluster = by_symbol.setdefault(
                primary.symbol,
                _Cluster(
                    article_ids=[],
                    article_indexes=[],
                    matches=[],
                    cluster_key=f"sym:{primary.market}:{primary.symbol}",
                ),
            )
            cluster.article_ids.append(art.id)
            cluster.article_indexes.append(idx)
            for m in matches:
                if m not in cluster.matches:
                    cluster.matches.append(m)
        else:
            words = _normalize_words(f"{art.title} {getattr(art, 'summary', '') or ''}")
            leftover_indexes.append(idx)
            leftover_words.append(words)
            leftover_shingles.append(_shingles(words))

    clusters: list[_Cluster] = list(by_symbol.values())

    # Greedy shingle clustering for leftovers.
    used = [False] * len(leftover_indexes)
    for i, shingles_i in enumerate(leftover_shingles):
        if used[i] or not shingles_i:
            continue
        used[i] = True
        members = [i]
        for j in range(i + 1, len(leftover_shingles)):
            if used[j] or not leftover_shingles[j]:
                continue
            inter = len(shingles_i & leftover_shingles[j])
            union = len(shingles_i | leftover_shingles[j])
            if union and inter / union >= 0.34:
                used[j] = True
                members.append(j)
        rep_words = leftover_words[i][:6] or ["topic"]
        key = "shg:" + "_".join(rep_words[:3])
        cluster = _Cluster(
            article_ids=[articles[leftover_indexes[m]].id for m in members],
            article_indexes=[leftover_indexes[m] for m in members],
            matches=[],
            cluster_key=key,
        )
        clusters.append(cluster)

    return clusters


def _stable_id(market: str, cluster_key: str, article_ids: Iterable[int]) -> str:
    """Deterministic 16-char ID for an issue cluster.

    Entity-keyed clusters (cluster_key starting with ``sym:``) are stable across
    new articles joining the cluster — their ID depends only on (market, key).
    Shingle clusters (``shg:``) include the sorted article-ID list because they
    have no natural identity beyond the articles themselves.
    """
    if cluster_key.startswith("sym:"):
        payload = f"{market}|{cluster_key}"
    else:
        payload = f"{market}|{cluster_key}|" + ",".join(
            str(i) for i in sorted(article_ids)
        )
    return hashlib.sha1(
        payload.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]


def _pick_issue_title(cluster: _Cluster, articles: list[NewsArticle]) -> str:
    if cluster.matches:
        return cluster.matches[0].canonical_name
    titles = [articles[i].title for i in cluster.article_indexes if articles[i].title]
    titles.sort(key=len)
    return titles[0] if titles else "Trending topic"


def _pick_subtitle(cluster: _Cluster, articles: list[NewsArticle]) -> str | None:
    titles = [articles[i].title for i in cluster.article_indexes]
    if len(titles) <= 1:
        return None
    return titles[1]


def _direction_from_titles(titles: list[str]) -> str:
    pos = sum(
        1
        for t in titles
        if _DIR_POS_RE.search(t) or any(w in t for w in _DIR_POS_KO)
    )
    neg = sum(
        1
        for t in titles
        if _DIR_NEG_RE.search(t) or any(w in t for w in _DIR_NEG_KO)
    )
    if pos and not neg:
        return "up"
    if neg and not pos:
        return "down"
    if pos and neg:
        return "mixed"
    return "neutral"


def _signals(
    cluster: _Cluster, articles: list[NewsArticle], window_hours: int
) -> IssueSignals:
    if not cluster.article_indexes:
        return IssueSignals(recency_score=0.0, source_diversity_score=0.0, mention_score=0.0)

    now = now_kst_naive()
    ages = []
    for idx in cluster.article_indexes:
        pub = articles[idx].article_published_at
        if pub is not None:
            mins = max(0, int((now - pub.replace(tzinfo=None)).total_seconds() / 60))
            ages.append(mins)
    if not ages:
        recency = 0.0
    else:
        newest = min(ages)
        recency = max(0.0, 1.0 - newest / max(1, window_hours * 60))

    sources = {articles[i].source for i in cluster.article_indexes if articles[i].source}
    source_diversity = min(1.0, len(sources) / 5.0)

    mention = min(1.0, math.log1p(len(cluster.article_indexes)) / math.log(10))

    return IssueSignals(
        recency_score=round(recency, 3),
        source_diversity_score=round(source_diversity, 3),
        mention_score=round(mention, 3),
    )


def _to_market_issue(
    *,
    cluster: _Cluster,
    articles: list[NewsArticle],
    market: str,
    window_hours: int,
    rank: int,
) -> MarketIssue:
    indexes = cluster.article_indexes
    cluster_articles = [articles[i] for i in indexes]
    signals = _signals(cluster, articles, window_hours)
    direction = _direction_from_titles([a.title for a in cluster_articles])

    related_symbols = [
        MarketIssueRelatedSymbol(
            symbol=m.symbol,
            market=m.market,
            canonical_name=m.canonical_name,
            mention_count=sum(
                1
                for a in cluster_articles
                if m.matched_term.lower()
                in f"{a.title or ''} {getattr(a, 'summary', '') or ''}".lower()
            ),
        )
        for m in cluster.matches
    ]

    sources = {a.source for a in cluster_articles if a.source}
    updated_at = max(
        (a.article_published_at for a in cluster_articles if a.article_published_at),
        default=now_kst_naive(),
    )

    issue_articles = [
        MarketIssueArticle(
            id=a.id,
            title=a.title,
            url=a.url,
            source=a.source,
            feed_source=a.feed_source,
            published_at=a.article_published_at,
            summary=getattr(a, "summary", None),
            matched_terms=[
                m.matched_term
                for m in match_symbols_for_article(
                    title=a.title,
                    summary=getattr(a, "summary", None),
                    keywords=getattr(a, "keywords", None) or [],
                    market=market if market != "all" else None,
                )
            ],
        )
        for a in cluster_articles
    ]

    issue_market = cluster_articles[0].market if cluster_articles else market
    if issue_market not in ("kr", "us", "crypto"):
        issue_market = "us"

    return MarketIssue(
        id=_stable_id(market, cluster.cluster_key, [a.id for a in cluster_articles]),
        market=issue_market,  # type: ignore[arg-type]
        rank=rank,
        issue_title=_pick_issue_title(cluster, articles),
        subtitle=_pick_subtitle(cluster, articles),
        direction=direction,  # type: ignore[arg-type]
        source_count=len(sources),
        article_count=len(cluster_articles),
        updated_at=updated_at,
        summary=None,
        related_symbols=related_symbols,
        related_sectors=[],
        articles=issue_articles,
        signals=signals,
    )


def _score(issue: MarketIssue) -> float:
    s = issue.signals
    return s.recency_score * 0.5 + s.source_diversity_score * 0.3 + s.mention_score * 0.2


async def build_market_issues(
    *,
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
    max_rows: int = 500,
) -> MarketIssuesResponse:
    """Build a ranked list of `MarketIssue` for a given market window."""
    articles = await _load_recent_articles(
        market=market, window_hours=window_hours, max_rows=max_rows
    )
    if not articles:
        return MarketIssuesResponse(
            market=market if market in ("kr", "us", "crypto", "all") else "all",  # type: ignore[arg-type]
            as_of=now_kst_naive(),
            window_hours=window_hours,
            items=[],
        )

    clusters = _cluster_articles(articles, market=market)
    issues = [
        _to_market_issue(
            cluster=c, articles=articles, market=market, window_hours=window_hours, rank=0
        )
        for c in clusters
        if c.article_indexes
    ]
    issues.sort(key=_score, reverse=True)
    issues = issues[:limit]
    for i, issue in enumerate(issues, start=1):
        issues[i - 1] = issue.model_copy(update={"rank": i})

    return MarketIssuesResponse(
        market=market if market in ("kr", "us", "crypto", "all") else "all",  # type: ignore[arg-type]
        as_of=now_kst_naive(),
        window_hours=window_hours,
        items=issues,
    )
