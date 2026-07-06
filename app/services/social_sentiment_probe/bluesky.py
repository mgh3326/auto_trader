from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.services.social_sentiment_probe.models import source_result, truncate_preview

HttpGet = Callable[..., Awaitable[Any]]
_SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


async def _default_get(url: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, **kwargs)


async def fetch_bluesky_posts(
    query: str,
    market: str,
    *,
    limit: int = 10,
    now: dt.datetime | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    try:
        get = http_get or _default_get
        response = await get(
            _SEARCH_URL,
            params={"q": query, "limit": max(1, min(limit, 100)), "sort": "latest"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return source_result(
            source="bluesky",
            market=market,
            query=query,
            status="unavailable",
            items=[],
            observed_at=observed_at,
            error_reason=f"{type(exc).__name__}: {exc}",
        )
    items = []
    for post in payload.get("posts", []):
        if not isinstance(post, dict):
            continue
        record = post.get("record") if isinstance(post.get("record"), dict) else {}
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        items.append(
            {
                "title": None,
                "url": post.get("uri"),
                "author": author.get("handle"),
                "published_at": record.get("createdAt") or post.get("indexedAt"),
                "text_preview": truncate_preview(record.get("text")),
                "metrics": {
                    "like_count": post.get("likeCount"),
                    "repost_count": post.get("repostCount"),
                    "reply_count": post.get("replyCount"),
                    "quote_count": post.get("quoteCount"),
                },
            }
        )
    return source_result(
        source="bluesky",
        market=market,
        query=query,
        status="ok",
        items=items,
        observed_at=observed_at,
    )
