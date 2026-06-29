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
from typing import Literal

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst_naive
from app.models.news import NewsArticle
from app.schemas.news_issues import (
    IssueQualityGate,
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
    MarketIssueRelatedSymbol,
    MarketIssuesResponse,
)
from app.services.market_news_noise import classify_title_noise
from app.services.news_entity_matcher import (
    SymbolMatch,
    match_symbols_for_article,
)
from app.services.news_text import NEWS_SUMMARY_MAX_CHARS, truncate_text

# ROB-502 meaningfulness gate: official/primary feeds whose items are market
# signals even as single-article clusters.
_IMPORTANT_FEED_SOURCES = {"rss_fed_press"}

# Token-set Jaccard threshold for merging near-duplicate shingle clusters
# (same story syndicated under slightly different titles).
_NEAR_DUP_TOKEN_JACCARD = 0.5

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
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "is",
    "are",
    "as",
    "at",
    "by",
    "with",
    "from",
    "이",
    "그",
    "저",
    "및",
    "는",
    "이는",
    "관련",
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
    return [
        w
        for w in (m.lower() for m in _WORD_RE.findall(text or ""))
        if w not in _STOPWORDS
    ]


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


def _cluster_articles(articles: list[NewsArticle], market: str) -> list[_Cluster]:
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
    return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


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
        1 for t in titles if _DIR_POS_RE.search(t) or any(w in t for w in _DIR_POS_KO)
    )
    neg = sum(
        1 for t in titles if _DIR_NEG_RE.search(t) or any(w in t for w in _DIR_NEG_KO)
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
        return IssueSignals(
            recency_score=0.0, source_diversity_score=0.0, mention_score=0.0
        )

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

    sources = {
        articles[i].source for i in cluster.article_indexes if articles[i].source
    }
    source_diversity = min(1.0, len(sources) / 5.0)

    mention = min(1.0, math.log1p(len(cluster.article_indexes)) / math.log(10))

    return IssueSignals(
        recency_score=round(recency, 3),
        source_diversity_score=round(source_diversity, 3),
        mention_score=round(mention, 3),
    )


def _article_summary_for_detail(
    raw_summary: str | None,
    detail: Literal["headline_only", "summary", "full"],
) -> str | None:
    """Shape a member-article summary by requested verbosity (ROB-628).

    headline_only -> drop; summary -> HTML-strip + <=NEWS_SUMMARY_MAX_CHARS;
    full -> verbatim (preserves the pre-ROB-628 contract).
    """
    if detail == "headline_only":
        return None
    if detail == "full":
        return raw_summary
    return truncate_text(raw_summary, NEWS_SUMMARY_MAX_CHARS)


def _to_market_issue(
    *,
    cluster: _Cluster,
    articles: list[NewsArticle],
    market: str,
    window_hours: int,
    rank: int,
    detail: Literal["headline_only", "summary", "full"] = "summary",
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
            summary=_article_summary_for_detail(getattr(a, "summary", None), detail),
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
    return (
        s.recency_score * 0.5 + s.source_diversity_score * 0.3 + s.mention_score * 0.2
    )


def _merge_near_duplicate_shingle_clusters(
    clusters: list[_Cluster], articles: list[NewsArticle]
) -> tuple[list[_Cluster], int]:
    """Merge shingle clusters whose title token sets overlap heavily.

    The 3-gram shingle pass misses syndicated copies of the same story with
    reworded titles (Jaccard on 3-grams collapses fast under word swaps); a
    token-set pass catches them. Entity-keyed clusters are never merged —
    distinct symbols are distinct issues by definition.
    """
    shingle = [c for c in clusters if not c.cluster_key.startswith("sym:")]
    others = [c for c in clusters if c.cluster_key.startswith("sym:")]
    token_sets = [
        set(
            _normalize_words(
                " ".join(articles[i].title or "" for i in c.article_indexes)
            )
        )
        for c in shingle
    ]
    merged_count = 0
    used = [False] * len(shingle)
    result: list[_Cluster] = []
    for i, cluster in enumerate(shingle):
        if used[i]:
            continue
        used[i] = True
        base = cluster
        for j in range(i + 1, len(shingle)):
            if used[j] or not token_sets[i] or not token_sets[j]:
                continue
            inter = len(token_sets[i] & token_sets[j])
            union = len(token_sets[i] | token_sets[j])
            if union and inter / union >= _NEAR_DUP_TOKEN_JACCARD:
                used[j] = True
                merged_count += 1
                other = shingle[j]
                base = _Cluster(
                    article_ids=base.article_ids + other.article_ids,
                    article_indexes=base.article_indexes + other.article_indexes,
                    matches=base.matches
                    + [m for m in other.matches if m not in base.matches],
                    cluster_key=base.cluster_key,
                )
        result.append(base)
    return others + result, merged_count


def _is_meaningful(issue: MarketIssue) -> bool:
    """ROB-502 gate: multi-article OR multi-source OR official-feed item.

    Single-article, single-source clusters are exactly the noise the
    2026-06-10 live audit surfaced in the US top-5 (one-off MarketWatch
    lifestyle pieces ranked as market issues purely on recency)."""
    if issue.article_count >= 2 or issue.source_count >= 2:
        return True
    return any((a.feed_source or "") in _IMPORTANT_FEED_SOURCES for a in issue.articles)


async def build_market_issues(
    *,
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
    max_rows: int = 500,
    detail: Literal["headline_only", "summary", "full"] = "summary",
) -> MarketIssuesResponse:
    """Build a ranked list of `MarketIssue` for a given market window.

    ROB-502: the output is meaningfulness-gated. ROB-628: `detail` controls
    member-article summary verbosity (headline_only/summary/full); default
    "summary" truncates each member summary to NEWS_SUMMARY_MAX_CHARS to keep
    MCP responses within the token budget.
    """
    response_market = market if market in ("kr", "us", "crypto", "all") else "all"
    loaded = await _load_recent_articles(
        market=market, window_hours=window_hours, max_rows=max_rows
    )
    if not loaded:
        return MarketIssuesResponse(
            market=response_market,  # type: ignore[arg-type]
            as_of=now_kst_naive(),
            window_hours=window_hours,
            items=[],
            status="no_recent_articles",
            degraded_reason=(
                f"no articles in the last {window_hours}h window — "
                "ingestion may be stale or paused"
            ),
            quality_gate=IssueQualityGate(),
        )

    articles = [a for a in loaded if not classify_title_noise(a.title or "")]
    noise_excluded = len(loaded) - len(articles)

    clusters = _cluster_articles(articles, market=market)
    clusters, merged_count = _merge_near_duplicate_shingle_clusters(clusters, articles)
    issues = [
        _to_market_issue(
            cluster=c,
            articles=articles,
            market=market,
            window_hours=window_hours,
            rank=0,
            detail=detail,
        )
        for c in clusters
        if c.article_indexes
    ]
    meaningful = [issue for issue in issues if _is_meaningful(issue)]
    excluded_thin = len(issues) - len(meaningful)

    meaningful.sort(key=_score, reverse=True)
    meaningful = meaningful[:limit]
    for i, issue in enumerate(meaningful, start=1):
        meaningful[i - 1] = issue.model_copy(update={"rank": i})

    gate = IssueQualityGate(
        articles_total=len(loaded),
        noise_articles_excluded=noise_excluded,
        clusters_total=len(issues),
        clusters_merged=merged_count,
        clusters_excluded_thin=excluded_thin,
    )
    status = "ok"
    degraded_reason = None
    if not meaningful:
        status = "no_meaningful_items"
        degraded_reason = (
            f"{len(loaded)} article(s) in window, but none formed a meaningful "
            f"cluster (noise_excluded={noise_excluded}, "
            f"thin_clusters={excluded_thin}) — no filler is generated"
        )

    return MarketIssuesResponse(
        market=response_market,  # type: ignore[arg-type]
        as_of=now_kst_naive(),
        window_hours=window_hours,
        items=meaningful,
        status=status,  # type: ignore[arg-type]
        degraded_reason=degraded_reason,
        quality_gate=gate,
    )
