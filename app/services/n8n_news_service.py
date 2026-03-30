"""N8n news service — fetches recent news with Discord-ready formatting."""

from __future__ import annotations

import logging
from datetime import datetime

from app.core.timezone import KST, now_kst
from app.schemas.n8n.news import (
    N8nNewsItem,
    N8nNewsResponse,
    N8nNewsSummary,
)
from app.services.llm_news_service import get_news_articles

logger = logging.getLogger(__name__)

_WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")
_CONTENT_PREVIEW_LEN = 300


def _truncate(text: str | None, max_len: int) -> str | None:
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _fmt_published(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.isoformat()


def _build_date_range(articles: list) -> str:
    """Build 'YYYY-MM-DD HH:MM ~ HH:MM' from article timestamps."""
    timestamps = [a.article_published_at for a in articles if a.article_published_at]
    if not timestamps:
        return ""
    earliest = min(timestamps)
    latest = max(timestamps)
    date_str = earliest.strftime("%Y-%m-%d")
    return f"{date_str} {earliest.strftime('%H:%M')} ~ {latest.strftime('%H:%M')}"


def _build_discord_title(total: int, as_of: datetime) -> str:
    weekday = _WEEKDAY_KR[as_of.weekday()]
    date_str = as_of.strftime("%Y-%m-%d")
    return f"📰 장전 뉴스 브리핑 ({date_str} {weekday}) — {total}건"


def _build_discord_body(items: list[N8nNewsItem], sources: list[str]) -> str:
    if not items:
        return "_뉴스가 없습니다._"

    lines: list[str] = []
    for item in items:
        source_tag = f"**[{item.source}]**" if item.source else "**[기타]**"
        # Use summary if available, else content_preview
        preview = item.summary or item.content_preview or ""
        lines.append(f"{source_tag} {item.title}")
        lines.append(f"<{item.url}>")
        if preview:
            lines.append(f"> {preview}")
        lines.append("")  # blank line between articles

    footer_sources = ", ".join(sources) if sources else "없음"
    lines.append("---")
    lines.append(f"_총 {len(items)}건 | 출처: {footer_sources}_")
    return "\n".join(lines)


async def fetch_n8n_news(
    hours: int = 2,
    feed_source: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> N8nNewsResponse:
    """Fetch recent news articles formatted for n8n → Discord posting."""
    as_of_dt = now_kst().replace(microsecond=0)
    as_of = as_of_dt.isoformat()

    try:
        articles, total = await get_news_articles(
            hours=hours,
            feed_source=feed_source,
            source=source,
            keyword=keyword,
            limit=limit,
        )

        items = [
            N8nNewsItem(
                id=a.id,
                title=a.title,
                url=a.url,
                source=a.source,
                feed_source=a.feed_source,
                summary=a.summary,
                content_preview=_truncate(a.article_content, _CONTENT_PREVIEW_LEN),
                published_at=_fmt_published(a.article_published_at),
                keywords=a.keywords,
                stock_symbol=a.stock_symbol,
                stock_name=a.stock_name,
            )
            for a in articles
        ]

        unique_sources = sorted({item.source for item in items if item.source})
        unique_feed_sources = sorted(
            {item.feed_source for item in items if item.feed_source}
        )

        summary = N8nNewsSummary(
            total=len(items),
            sources=unique_sources,
            feed_sources=unique_feed_sources,
            date_range=_build_date_range(articles),
        )

        discord_title = _build_discord_title(len(items), as_of_dt)
        discord_body = _build_discord_body(items, unique_sources)

        return N8nNewsResponse(
            success=True,
            as_of=as_of,
            summary=summary,
            items=items,
            discord_title=discord_title,
            discord_body=discord_body,
        )

    except Exception as exc:
        logger.exception("Failed to fetch n8n news")
        return N8nNewsResponse(
            success=False,
            as_of=as_of,
            summary=N8nNewsSummary(total=0, sources=[], date_range=""),
            items=[],
            discord_title="",
            discord_body="",
            errors=[{"error": str(exc)}],
        )
