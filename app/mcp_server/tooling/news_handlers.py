from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from app.models.news import NewsArticle
from app.services.crypto_news_relevance_service import (
    rank_crypto_news_for_briefing,
    score_crypto_news_article,
)
from app.services.llm_news_service import get_news_articles
from app.services.market_news_briefing_formatter import (
    BriefingSection,
    format_market_news_briefing,
)
from app.services.market_news_noise import classify_title_noise, noise_reason
from app.services.news_text import (
    NEWS_RESPONSE_MAX_CHARS,
    NEWS_SUMMARY_MAX_CHARS,
    truncate_text,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

NEWS_TOOL_NAMES = ["get_market_news", "get_market_issues"]


def _article_to_dict(
    article: NewsArticle,
    *,
    detail: str = "summary",
    include_crypto_relevance: bool = False,
    briefing_relevance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "feed_source": article.feed_source,
        "market": article.market,
        "published_at": article.article_published_at.isoformat()
        if article.article_published_at
        else None,
        "keywords": article.keywords,
        "stock_symbol": article.stock_symbol,
        "stock_name": article.stock_name,
    }
    # detail controls the body field: headline_only omits it entirely; full keeps
    # the raw summary; summary (default) HTML-strips + caps to NEWS_SUMMARY_MAX_CHARS.
    if detail == "headline_only":
        pass
    elif detail == "full":
        item["summary"] = article.summary
    else:  # "summary" (default / unknown -> safe default)
        item["summary"] = truncate_text(article.summary, NEWS_SUMMARY_MAX_CHARS)
    if include_crypto_relevance:
        item["crypto_relevance"] = score_crypto_news_article(article).as_dict()
    if briefing_relevance is not None:
        item["briefing_relevance"] = briefing_relevance
    return item


def _briefing_sections_to_dict(
    sections: list[BriefingSection],
) -> list[dict[str, Any]]:
    # ROB-628: sections carry only article ids + per-article relevance. The full
    # article bodies are emitted exactly once, in news[]. This stops the response
    # from re-embedding every article dict per section.
    return [
        {
            "section_id": section.section_id,
            "title": section.title,
            "count": len(section.items),
            "article_ids": [item.article.id for item in section.items],
            "relevance": [item.relevance.as_dict() for item in section.items],
        }
        for section in sections
    ]


async def _get_market_news_impl(
    market: str | None = None,
    hours: int | None = 24,
    feed_source: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    limit: int | None = 20,
    briefing_filter: bool = False,
    detail: str = "summary",
) -> dict[str, Any]:
    hours = hours or 24
    limit = limit or 20

    query_limit = limit
    if market in {"crypto", "us", "kr"} and briefing_filter:
        # Pull a slightly larger window so ranking can hide low-signal noise
        # without returning an under-filled briefing when relevant items exist.
        query_limit = max(limit * 3, limit)

    articles, total = await get_news_articles(
        market=market,
        hours=hours,
        feed_source=feed_source,
        source=source,
        keyword=keyword,
        limit=query_limit,
    )

    # ROB-502 quality gate (always on): noise-classified titles never reach
    # the default list — they move to excluded_news with an explicit reason.
    gated_articles = []
    noise_excluded: list[dict[str, Any]] = []
    for article in articles:
        noise = classify_title_noise(article.title or "")
        if noise:
            item = _article_to_dict(article, detail=detail)
            item["excluded_reason"] = noise_reason(noise)
            noise_excluded.append(item)
        else:
            gated_articles.append(article)
    articles = gated_articles

    excluded_news: list[dict[str, Any]] = []
    briefing_summary = None
    briefing_sections: list[dict[str, Any]] = []
    if market == "crypto":
        if briefing_filter:
            ranking = rank_crypto_news_for_briefing(list(articles), limit=limit)
            news_list = [
                _article_to_dict(
                    item.article, detail=detail, include_crypto_relevance=True
                )
                for item in ranking.included
            ]
            excluded_news = [
                _article_to_dict(
                    item.article, detail=detail, include_crypto_relevance=True
                )
                for item in ranking.excluded
            ]
            briefing_summary = ranking.summary
            briefing = format_market_news_briefing(
                list(articles), market=market, limit=limit
            )
            briefing_sections = _briefing_sections_to_dict(briefing.sections)
        else:
            news_list = [
                _article_to_dict(a, detail=detail, include_crypto_relevance=True)
                for a in articles
            ]
    elif briefing_filter and market in {"us", "kr"}:
        briefing = format_market_news_briefing(
            list(articles), market=market, limit=limit
        )
        news_list = [
            _article_to_dict(
                item.article,
                detail=detail,
                briefing_relevance=item.relevance.as_dict(),
            )
            for section in briefing.sections
            for item in section.items
        ]
        excluded_news = [
            _article_to_dict(
                item.article,
                detail=detail,
                briefing_relevance=item.relevance.as_dict(),
            )
            for item in briefing.excluded
        ]
        briefing_summary = briefing.summary
        briefing_sections = _briefing_sections_to_dict(briefing.sections)
    else:
        news_list = [_article_to_dict(a, detail=detail) for a in articles]
    excluded_news = noise_excluded + excluded_news
    # ROB-628: cap excluded_news to `limit`; excluded_total keeps the true count.
    excluded_total = len(excluded_news)
    excluded_news = excluded_news[:limit]
    source_names = list({a.get("source") for a in news_list if a.get("source")})
    feed_source_names = list(
        {a.get("feed_source") for a in news_list if a.get("feed_source")}
    )

    # ROB-502: degraded states are explicit — no filler when nothing passes.
    status = "ok"
    degraded_reason = None
    if total == 0:
        status = "no_recent_articles"
        degraded_reason = (
            f"no articles in the last {hours}h window — "
            "ingestion may be stale or paused"
        )
    elif not news_list:
        status = "no_meaningful_items"
        degraded_reason = (
            f"{total} article(s) in window, but none passed the quality gate "
            f"({excluded_total} excluded — see excluded_news reasons); "
            "no filler is generated"
        )

    payload: dict[str, Any] = {
        "surface": "quality_gated_market_briefing",
        "advisory": (
            "Quality-gated broad-market DB-backed surface for briefing only; "
            "NOT investment-decision evidence. Use get_news for one symbol's "
            "catalysts, or get_holdings_news to sweep your holdings' catalysts "
            "in one call (ROB-628). Noise-classified items appear in "
            "excluded_news with reasons instead of the main list."
        ),
        "market": market,
        "status": status,
        "degraded_reason": degraded_reason,
        "count": len(news_list),
        "total": total,
        "news": news_list,
        "sources": sorted(source_names),
        "feed_sources": sorted(feed_source_names),
        "briefing_filter": bool(briefing_filter),
        "briefing_summary": briefing_summary,
        "briefing_sections": briefing_sections,
        "excluded_news": excluded_news,
        "excluded_total": excluded_total,
        "truncated_for_size": False,
    }

    return _apply_size_cap(payload)


def _apply_size_cap(payload: dict[str, Any]) -> dict[str, Any]:
    # ROB-628: hard size cap. If the serialized response exceeds
    # NEWS_RESPONSE_MAX_CHARS, drop excluded_news first (least decision-critical),
    # then trailing news[] items, until under the cap. Never silently: set
    # truncated_for_size, append to degraded_reason, and report counts.
    if len(json.dumps(payload, default=str)) <= NEWS_RESPONSE_MAX_CHARS:
        return payload

    dropped_excluded = len(payload["excluded_news"])
    payload["excluded_news"] = []

    dropped_news = 0
    while (
        len(json.dumps(payload, default=str)) > NEWS_RESPONSE_MAX_CHARS
        and payload["news"]
    ):
        payload["news"].pop()
        dropped_news += 1

    payload["count"] = len(payload["news"])
    payload["truncated_for_size"] = True
    payload["size_truncation"] = {
        "dropped_news": dropped_news,
        "dropped_excluded": dropped_excluded,
        "response_chars": len(json.dumps(payload, default=str)),
        "max_chars": NEWS_RESPONSE_MAX_CHARS,
    }
    reason = (
        f"response exceeded {NEWS_RESPONSE_MAX_CHARS} chars — dropped "
        f"{dropped_excluded} excluded and {dropped_news} trailing news item(s) "
        "to fit (use detail='headline_only' or a smaller limit for the full set)"
    )
    payload["degraded_reason"] = (
        f"{payload['degraded_reason']}; {reason}"
        if payload.get("degraded_reason")
        else reason
    )
    if payload["status"] == "ok":
        payload["status"] = "truncated_for_size"
    return payload


async def _get_market_issues_impl(
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
    detail: Literal["headline_only", "summary", "full"] = "summary",
) -> dict[str, Any]:
    from app.services.news_issue_clustering_service import build_market_issues

    response = await build_market_issues(
        market=market, window_hours=window_hours, limit=limit, detail=detail
    )
    payload = response.model_dump(mode="json")
    return _enforce_market_issues_size_cap(payload)


def _enforce_market_issues_size_cap(payload: dict[str, Any]) -> dict[str, Any]:
    """Hard cap the serialized response at NEWS_RESPONSE_MAX_CHARS (ROB-628).

    Trims in three deterministic passes — (1) collapse trailing issues to a
    single anchor article, (2) drop whole trailing issues (always keep >=1),
    (3) collapse the survivors' articles as a last resort — then flips
    truncated_for_size and appends a counted degraded_reason. Never silently
    drops or fabricates: every removal is reflected in the flag + reason.
    """

    def _encoded_len() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    if _encoded_len() <= NEWS_RESPONSE_MAX_CHARS:
        return payload

    items = payload.get("items") or []
    original_issue_count = len(items)
    original_article_count = sum(len(it.get("articles") or []) for it in items)

    # Pass 1: trim trailing member articles down to a single anchor article.
    for item in reversed(items):
        if _encoded_len() <= NEWS_RESPONSE_MAX_CHARS:
            break
        arts = item.get("articles") or []
        if len(arts) > 1:
            item["articles"] = arts[:1]

    # Pass 2: drop whole trailing issues (keep at least one) until under cap.
    while len(items) > 1 and _encoded_len() > NEWS_RESPONSE_MAX_CHARS:
        items.pop()

    # Pass 3: last resort — collapse any remaining multi-article survivors.
    if _encoded_len() > NEWS_RESPONSE_MAX_CHARS:
        for item in items:
            arts = item.get("articles") or []
            if len(arts) > 1:
                item["articles"] = arts[:1]

    payload["items"] = items
    kept_issue_count = len(items)
    kept_article_count = sum(len(it.get("articles") or []) for it in items)
    dropped_issues = original_issue_count - kept_issue_count
    dropped_articles = original_article_count - kept_article_count

    payload["truncated_for_size"] = True
    reason = (
        f"response exceeded the {NEWS_RESPONSE_MAX_CHARS}-char size cap; trimmed "
        f"{dropped_issues} issue(s) and {dropped_articles} member article(s) to "
        "fit — re-query with a narrower market/window or detail='headline_only' "
        "for the full set"
    )
    existing = payload.get("degraded_reason")
    payload["degraded_reason"] = f"{existing}; {reason}" if existing else reason
    return payload


def _register_news_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_market_news",
        description=(
            "[Quality-gated broad market briefing surface; NOT investment-decision "
            "evidence — use get_news for symbol-level decisions] "
            "Get recent market news with a noise gate always on (ROB-502): "
            "personal-finance/lifestyle/sponsored/price-prediction/broad-tech items "
            "move to excluded_news with an excluded_reason instead of the main list. "
            "status is 'ok' | 'no_meaningful_items' | 'no_recent_articles' | "
            "'truncated_for_size' with degraded_reason — no filler is generated. "
            "detail controls per-article body: 'headline_only' (no summary), "
            "'summary' (default, HTML-stripped + capped to 240 chars), or 'full' "
            "(raw untruncated). briefing_sections carry only article_ids + relevance; "
            "bodies live once in news[]. excluded_news is capped to limit "
            "(excluded_total = true count). Oversized responses set truncated_for_size "
            "and drop trailing items rather than overflow. Supports filtering by "
            "market, publisher (source), collection path (feed_source), and keyword. "
            "briefing_filter=True additionally formats market-specific sections for "
            "kr/us and ranks crypto-relevant items."
        ),
    )
    async def get_market_news(
        market: str | None = None,
        hours: int = 24,
        feed_source: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        briefing_filter: bool = False,
        detail: Literal["headline_only", "summary", "full"] = "summary",
    ) -> dict[str, Any]:
        return await _get_market_news_impl(
            market=market,
            hours=hours,
            feed_source=feed_source,
            source=source,
            keyword=keyword,
            limit=limit,
            briefing_filter=briefing_filter,
            detail=detail,
        )

    @mcp.tool(
        name="get_market_issues",
        description=(
            "Read-only deterministic market issue clusters from collected news "
            "(ROB-130, quality-gated per ROB-502). Groups recent articles by "
            "entity/topic, merges near-duplicate syndicated stories, and ranks by "
            "recency + source diversity + mention count. Noise-classified articles "
            "never enter clustering, and thin clusters (single article AND single "
            "source, non-official feed) are withheld. status/degraded_reason/"
            "quality_gate report what the gate did; empty results are explicit "
            "(no_meaningful_items), never filler. detail controls member-article "
            "summary verbosity: 'headline_only' drops summaries, 'summary' "
            "(default) truncates each to 240 chars, 'full' keeps them verbatim. "
            "The response is hard-capped at 8000 chars; if exceeded, trailing "
            "issues/articles are trimmed and truncated_for_size + degraded_reason "
            "are set (never a silent drop)."
        ),
    )
    async def get_market_issues(
        market: str = "all",
        window_hours: int = 24,
        limit: int = 20,
        detail: Literal["headline_only", "summary", "full"] = "summary",
    ) -> dict[str, Any]:
        return await _get_market_issues_impl(
            market=market, window_hours=window_hours, limit=limit, detail=detail
        )
