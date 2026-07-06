from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.services.social_sentiment_probe.models import source_result, truncate_preview

HttpPost = Callable[..., Awaitable[Any]]
HttpGet = Callable[..., Awaitable[Any]]
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


async def _default_post(url: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(url, **kwargs)


async def _default_get(url: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, **kwargs)


async def _token(
    client_id: str,
    client_secret: str,
    user_agent: str,
    post: HttpPost,
) -> str:
    response = await post(
        _TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": user_agent},
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Reddit access_token missing")
    token_type = payload.get("token_type") or "bearer"
    return f"{token_type} {token}"


async def fetch_reddit_search(
    query: str,
    market: str,
    client_id: str | None,
    client_secret: str | None,
    user_agent: str | None,
    *,
    subreddits: tuple[str, ...] = (),
    limit: int = 10,
    now: dt.datetime | None = None,
    post: HttpPost | None = None,
    get: HttpGet | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    if not client_id or not client_secret or not user_agent:
        return source_result(
            source="reddit",
            market=market,
            query=query,
            status="missing_credentials",
            items=[],
            observed_at=observed_at,
            error_reason="REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT are required",
        )
    try:
        post_fn = post or _default_post
        get_fn = get or _default_get
        auth_header = await _token(client_id, client_secret, user_agent, post_fn)
        search_payloads = []
        for sr in subreddits or (None,):
            url = (
                f"https://oauth.reddit.com/r/{sr}/search"
                if sr
                else "https://oauth.reddit.com/search"
            )
            response = await get_fn(
                url,
                params={
                    "q": query,
                    "restrict_sr": bool(sr),
                    "sort": "new",
                    "limit": max(1, min(limit, 100)),
                    "raw_json": 1,
                },
                headers={"Authorization": auth_header, "User-Agent": user_agent},
            )
            response.raise_for_status()
            search_payloads.append(response.json())
    except Exception as exc:
        return source_result(
            source="reddit",
            market=market,
            query=query,
            status="unavailable",
            items=[],
            observed_at=observed_at,
            error_reason=f"{type(exc).__name__}: {exc}",
        )
    items = []
    for payload in search_payloads:
        for child in payload.get("data", {}).get("children", []):
            data = child.get("data") if isinstance(child, dict) else {}
            if not isinstance(data, dict):
                continue
            permalink = data.get("permalink")
            items.append(
                {
                    "title": data.get("title"),
                    "url": f"https://www.reddit.com{permalink}"
                    if permalink
                    else data.get("url"),
                    "author": data.get("author"),
                    "published_at": data.get("created_utc"),
                    "text_preview": truncate_preview(data.get("selftext")),
                    "metrics": {
                        "score": data.get("score"),
                        "comment_count": data.get("num_comments"),
                    },
                }
            )
    return source_result(
        source="reddit",
        market=market,
        query=query,
        status="ok",
        items=items,
        observed_at=observed_at,
        quota={"documented_oauth_requests_per_minute": 60},
    )
