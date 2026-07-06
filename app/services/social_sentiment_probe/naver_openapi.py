from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.services.social_sentiment_probe.models import (
    source_result,
    strip_markup,
    truncate_preview,
)

HttpGet = Callable[..., Awaitable[Any]]

_ENDPOINTS = {
    "news": "https://openapi.naver.com/v1/search/news.json",
    "blog": "https://openapi.naver.com/v1/search/blog.json",
    "cafearticle": "https://openapi.naver.com/v1/search/cafearticle.json",
}


async def _default_get(url: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, **kwargs)


async def fetch_naver_openapi(
    kind: str,
    query: str,
    market: str,
    client_id: str | None,
    client_secret: str | None,
    *,
    display: int = 10,
    now: dt.datetime | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    source = f"naver_openapi_{kind}"
    if kind not in _ENDPOINTS:
        return source_result(
            source=source,
            market=market,
            query=query,
            status="unsupported_kind",
            items=[],
            observed_at=observed_at,
            error_reason=f"unsupported Naver Search kind: {kind}",
        )
    if not client_id or not client_secret:
        return source_result(
            source=source,
            market=market,
            query=query,
            status="missing_credentials",
            items=[],
            observed_at=observed_at,
            error_reason="NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required",
        )
    try:
        get = http_get or _default_get
        response = await get(
            _ENDPOINTS[kind],
            params={
                "query": query,
                "display": max(1, min(display, 100)),
                "sort": "date",
            },
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return source_result(
            source=source,
            market=market,
            query=query,
            status="unavailable",
            items=[],
            observed_at=observed_at,
            error_reason=f"{type(exc).__name__}: {exc}",
        )
    items = []
    for row in payload.get("items", []):
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "title": strip_markup(row.get("title")),
                "url": row.get("originallink") or row.get("link"),
                "author": row.get("bloggername") or row.get("cafename"),
                "published_at": row.get("pubDate") or row.get("postdate"),
                "text_preview": truncate_preview(strip_markup(row.get("description"))),
                "metrics": {},
            }
        )
    return source_result(
        source=source,
        market=market,
        query=query,
        status="ok",
        items=items,
        observed_at=observed_at,
        quota={"documented_daily_limit": 25000},
    )
