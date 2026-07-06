# ROB-729 Free Social Sources v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an operator-only, zero-write probe for free SNS and public-opinion sources that emits `evidence_snapshot["social_sentiment"]` JSON for manual trading-session use.

**Architecture:** Keep v0 outside production request paths, schedulers, DB writes, and order flows. Add small source adapters under `app/services/social_sentiment_probe/`, then orchestrate them from one `scripts/free_social_sources_probe.py` CLI that prints a normalized evidence envelope. Existing MCP `get_retail_sentiment` and remote-debug CDP utilities are reused instead of reintroducing duplicate Naver discussion or Chrome protocol logic.

**Tech Stack:** Python 3.13, `uv`, `httpx`, `argparse`, pytest with hermetic fake HTTP/CDP clients, existing `app.services.action_report.remote_debug_audit.CdpClient`.

## Global Constraints

- Runtime baseline is Python 3.13+; run commands through `uv`.
- No hardcoded credentials or secrets. Read only these env vars: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `STOCKTWITS_FIRESTREAM_USERNAME`, `STOCKTWITS_FIRESTREAM_PASSWORD`.
- v0 is operator-only: no FastAPI routes, no TaskIQ tasks, no Alembic migrations, no DB writes, no broker calls, no watch/order/order-intent mutations.
- Social and opinion data are advisory evidence only. Do not create automatic include/exclude or buy/sell decisions from this data.
- Naver discussion remains aggregate-only. Do not store or print raw Naver discussion post titles, bodies, authors, nicknames, or comment text.
- X via CDP is read-only, low-frequency, opt-in with `--include-x-cdp`, and host-locked through existing `CdpClient` at `127.0.0.1:9222`.
- StockTwits is a condition check in v0. Official Firestream docs require Basic auth, so unauthenticated scraping is out of scope.
- External API facts checked 2026-07-06:
  - Naver Search API is non-login OpenAPI and uses `X-Naver-Client-Id` / `X-Naver-Client-Secret`; news and cafe docs state a 25,000 daily quota.
  - Reddit API clients must authenticate with OAuth2, use a descriptive user agent, and the archived official API rules list 60 OAuth requests per minute.
  - Bluesky `public.api.bsky.app` is the public cached AppView endpoint and direct endpoints do not support authentication.
  - StockTwits Firestream symbol stream requires Basic Authentication.

---

## File Structure

- Create `app/services/social_sentiment_probe/__init__.py`
  - Package marker and public exports.
- Create `app/services/social_sentiment_probe/models.py`
  - Pure normalization helpers and the `social_sentiment` evidence envelope builder.
- Create `app/services/social_sentiment_probe/naver_openapi.py`
  - Naver Search OpenAPI adapters for `news`, `blog`, and `cafearticle`.
- Create `app/services/social_sentiment_probe/bluesky.py`
  - Bluesky public AppView `app.bsky.feed.searchPosts` adapter.
- Create `app/services/social_sentiment_probe/reddit.py`
  - Reddit OAuth token and read-only search adapter.
- Create `app/services/social_sentiment_probe/stocktwits.py`
  - StockTwits Firestream credential/status probe. Do not add unauthenticated scraping.
- Create `app/services/social_sentiment_probe/x_cdp.py`
  - Optional X search-page reader using existing CDP session abstraction.
- Create `scripts/free_social_sources_probe.py`
  - Operator CLI that selects sources by market and prints JSON.
- Create `tests/services/social_sentiment_probe/test_models.py`
  - Evidence envelope and sanitization unit tests.
- Create `tests/services/social_sentiment_probe/test_sources.py`
  - Hermetic source parser/fail-open tests.
- Create `tests/services/social_sentiment_probe/test_cli.py`
  - CLI parser and orchestration tests with fake source runners.
- Create `docs/runbooks/free-social-sources-v0.md`
  - Operator setup, env vars, examples, live smoke checks, and governance.

## Task 1: Evidence Envelope and Sanitizers

**Files:**
- Create: `app/services/social_sentiment_probe/__init__.py`
- Create: `app/services/social_sentiment_probe/models.py`
- Test: `tests/services/social_sentiment_probe/test_models.py`

**Interfaces:**
- Produces: `strip_markup(value: str | None) -> str | None`
- Produces: `truncate_preview(value: str | None, limit: int = 280) -> str | None`
- Produces: `source_result(source: str, market: str, query: str, status: str, items: list[dict[str, Any]], observed_at: datetime, error_reason: str | None = None, quota: dict[str, Any] | None = None) -> dict[str, Any]`
- Produces: `build_social_sentiment_evidence(market: str, symbol: str, query: str, source_results: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/social_sentiment_probe/test_models.py`:

```python
from __future__ import annotations

import datetime as dt

from app.services.social_sentiment_probe.models import (
    build_social_sentiment_evidence,
    source_result,
    strip_markup,
    truncate_preview,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 7, 6, 1, 2, 3, tzinfo=dt.UTC)


def test_strip_markup_removes_naver_b_tags_and_unescapes_entities() -> None:
    assert strip_markup("<b>삼성전자</b> &amp; SK하이닉스") == "삼성전자 & SK하이닉스"


def test_truncate_preview_preserves_short_values_and_caps_long_values() -> None:
    assert truncate_preview("abc", limit=3) == "abc"
    assert truncate_preview("abcdef", limit=3) == "abc"
    assert truncate_preview(None) is None


def test_source_result_counts_items_and_omits_empty_error() -> None:
    out = source_result(
        source="bluesky",
        market="us",
        query="AAPL",
        status="ok",
        items=[{"title": "AAPL"}],
        observed_at=_now(),
    )
    assert out["source"] == "bluesky"
    assert out["item_count"] == 1
    assert out["observed_at"] == "2026-07-06T01:02:03+00:00"
    assert "error_reason" not in out


def test_build_social_sentiment_evidence_is_advisory_and_zero_cost() -> None:
    src = source_result(
        source="reddit",
        market="us",
        query="NVDA",
        status="ok",
        items=[{"title": "NVDA volume"}],
        observed_at=_now(),
    )
    out = build_social_sentiment_evidence(
        market="us",
        symbol="NVDA",
        query="NVDA",
        source_results=[src],
        observed_at=_now(),
    )
    assert out["source"] == "free_social_sources_v0"
    assert out["advisory_only"] is True
    assert out["cost_usd"] == 0
    assert out["summary"] == {
        "source_count": 1,
        "ok_source_count": 1,
        "total_item_count": 1,
    }
    assert out["sources"] == [src]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/services/social_sentiment_probe/test_models.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.social_sentiment_probe'`.

- [ ] **Step 3: Add the package and model helpers**

Create `app/services/social_sentiment_probe/__init__.py`:

```python
"""Operator-only free social/opinion source probe helpers (ROB-729)."""
```

Create `app/services/social_sentiment_probe/models.py`:

```python
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")


def strip_markup(value: str | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(_TAG_RE.sub("", value))
    return " ".join(text.split())


def truncate_preview(value: str | None, limit: int = 280) -> str | None:
    if value is None:
        return None
    clean = " ".join(value.split())
    return clean[: max(0, limit)]


def source_result(
    *,
    source: str,
    market: str,
    query: str,
    status: str,
    items: list[dict[str, Any]],
    observed_at: dt.datetime,
    error_reason: str | None = None,
    quota: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "market": market,
        "query": query,
        "status": status,
        "observed_at": observed_at.isoformat(),
        "item_count": len(items),
        "items": items,
    }
    if error_reason:
        payload["error_reason"] = error_reason
    if quota:
        payload["quota"] = quota
    return payload


def build_social_sentiment_evidence(
    *,
    market: str,
    symbol: str,
    query: str,
    source_results: list[dict[str, Any]],
    observed_at: dt.datetime,
) -> dict[str, Any]:
    ok_count = sum(1 for result in source_results if result.get("status") == "ok")
    total_items = sum(int(result.get("item_count") or 0) for result in source_results)
    return {
        "source": "free_social_sources_v0",
        "market": market,
        "symbol": symbol,
        "query": query,
        "observed_at": observed_at.isoformat(),
        "advisory_only": True,
        "cost_usd": 0,
        "summary": {
            "source_count": len(source_results),
            "ok_source_count": ok_count,
            "total_item_count": total_items,
        },
        "sources": source_results,
    }
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/services/social_sentiment_probe/test_models.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/social_sentiment_probe/__init__.py app/services/social_sentiment_probe/models.py tests/services/social_sentiment_probe/test_models.py
git commit -m "feat(rob-729): add social sentiment evidence envelope"
```

## Task 2: Free API Source Readers

**Files:**
- Create: `app/services/social_sentiment_probe/naver_openapi.py`
- Create: `app/services/social_sentiment_probe/bluesky.py`
- Create: `app/services/social_sentiment_probe/reddit.py`
- Create: `app/services/social_sentiment_probe/stocktwits.py`
- Test: `tests/services/social_sentiment_probe/test_sources.py`

**Interfaces:**
- Consumes: `source_result`, `strip_markup`, `truncate_preview`
- Produces: `fetch_naver_openapi(kind: str, query: str, market: str, client_id: str | None, client_secret: str | None, display: int = 10, now: datetime | None = None, http_get: HttpGet | None = None) -> dict[str, Any]`
- Produces: `fetch_bluesky_posts(query: str, market: str, limit: int = 10, now: datetime | None = None, http_get: HttpGet | None = None) -> dict[str, Any]`
- Produces: `fetch_reddit_search(query: str, market: str, client_id: str | None, client_secret: str | None, user_agent: str | None, subreddits: tuple[str, ...] = (), limit: int = 10, now: datetime | None = None, post: HttpPost | None = None, get: HttpGet | None = None) -> dict[str, Any]`
- Produces: `probe_stocktwits_firestream(symbol: str, market: str, username: str | None, password: str | None, now: datetime | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write the failing source tests**

Create `tests/services/social_sentiment_probe/test_sources.py`:

```python
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.services.social_sentiment_probe.bluesky import fetch_bluesky_posts
from app.services.social_sentiment_probe.naver_openapi import fetch_naver_openapi
from app.services.social_sentiment_probe.reddit import fetch_reddit_search
from app.services.social_sentiment_probe.stocktwits import probe_stocktwits_firestream


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"x-ratelimit-remaining": "59"}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _now() -> dt.datetime:
    return dt.datetime(2026, 7, 6, 1, 2, 3, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_naver_openapi_missing_credentials_is_fail_open() -> None:
    out = await fetch_naver_openapi(
        "news", "삼성전자", "kr", None, None, now=_now()
    )
    assert out["status"] == "missing_credentials"
    assert out["item_count"] == 0
    assert out["source"] == "naver_openapi_news"


@pytest.mark.asyncio
async def test_naver_openapi_parses_items_without_html_markup() -> None:
    async def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        assert url.endswith("/v1/search/news.json")
        assert kwargs["headers"]["X-Naver-Client-Id"] == "cid"
        return FakeResponse(
            {
                "items": [
                    {
                        "title": "<b>삼성전자</b> 수급",
                        "description": "외국인 &amp; 기관 순매수",
                        "originallink": "https://example.com/a",
                        "pubDate": "Mon, 06 Jul 2026 09:00:00 +0900",
                    }
                ]
            }
        )

    out = await fetch_naver_openapi(
        "news", "삼성전자", "kr", "cid", "secret", now=_now(), http_get=fake_get
    )
    assert out["status"] == "ok"
    assert out["items"][0]["title"] == "삼성전자 수급"
    assert out["items"][0]["text_preview"] == "외국인 & 기관 순매수"


@pytest.mark.asyncio
async def test_bluesky_parses_public_search_posts() -> None:
    async def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        assert url == "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
        assert kwargs["params"]["q"] == "AAPL"
        return FakeResponse(
            {
                "posts": [
                    {
                        "uri": "at://did/app.bsky.feed.post/abc",
                        "author": {"handle": "trader.example"},
                        "record": {"text": "AAPL breakout", "createdAt": "2026-07-06T00:00:00Z"},
                        "likeCount": 3,
                        "repostCount": 1,
                        "replyCount": 2,
                        "quoteCount": 0,
                    }
                ]
            }
        )

    out = await fetch_bluesky_posts("AAPL", "us", now=_now(), http_get=fake_get)
    assert out["status"] == "ok"
    assert out["items"][0]["author"] == "trader.example"
    assert out["items"][0]["metrics"]["like_count"] == 3


@pytest.mark.asyncio
async def test_reddit_missing_credentials_is_fail_open() -> None:
    out = await fetch_reddit_search(
        "NVDA", "us", None, None, "auto_trader.rob729/0.1", now=_now()
    )
    assert out["status"] == "missing_credentials"
    assert out["items"] == []


@pytest.mark.asyncio
async def test_reddit_parses_listing_children() -> None:
    async def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        assert url == "https://www.reddit.com/api/v1/access_token"
        assert kwargs["data"] == {"grant_type": "client_credentials"}
        return FakeResponse({"access_token": "tok", "token_type": "bearer"})

    async def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        assert url == "https://oauth.reddit.com/r/stocks/search"
        assert kwargs["headers"]["Authorization"] == "bearer tok"
        return FakeResponse(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "NVDA earnings",
                                "author": "analyst_user",
                                "permalink": "/r/stocks/comments/1/nvda/",
                                "created_utc": 1783300000,
                                "score": 12,
                                "num_comments": 4,
                                "selftext": "AI demand",
                            }
                        }
                    ]
                }
            }
        )

    out = await fetch_reddit_search(
        "NVDA",
        "us",
        "cid",
        "secret",
        "script:auto_trader.rob729:v0.1 (by /u/example)",
        subreddits=("stocks",),
        now=_now(),
        post=fake_post,
        get=fake_get,
    )
    assert out["status"] == "ok"
    assert out["items"][0]["url"] == "https://www.reddit.com/r/stocks/comments/1/nvda/"
    assert out["items"][0]["metrics"] == {"score": 12, "comment_count": 4}


def test_stocktwits_without_credentials_reports_requires_credentials() -> None:
    out = probe_stocktwits_firestream("AAPL", "us", None, None, now=_now())
    assert out["status"] == "requires_credentials"
    assert out["source"] == "stocktwits_firestream"
    assert out["items"] == []
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/services/social_sentiment_probe/test_sources.py -v`

Expected: FAIL with `ModuleNotFoundError` for the new source modules.

- [ ] **Step 3: Implement Naver OpenAPI reader**

Create `app/services/social_sentiment_probe/naver_openapi.py`:

```python
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
            params={"query": query, "display": max(1, min(display, 100)), "sort": "date"},
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
```

- [ ] **Step 4: Implement Bluesky reader**

Create `app/services/social_sentiment_probe/bluesky.py`:

```python
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
```

- [ ] **Step 5: Implement Reddit reader**

Create `app/services/social_sentiment_probe/reddit.py`:

```python
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
                    "url": f"https://www.reddit.com{permalink}" if permalink else data.get("url"),
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
```

- [ ] **Step 6: Implement StockTwits condition probe**

Create `app/services/social_sentiment_probe/stocktwits.py`:

```python
from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.social_sentiment_probe.models import source_result


def probe_stocktwits_firestream(
    symbol: str,
    market: str,
    username: str | None,
    password: str | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    if not username or not password:
        return source_result(
            source="stocktwits_firestream",
            market=market,
            query=symbol,
            status="requires_credentials",
            items=[],
            observed_at=observed_at,
            error_reason=(
                "Official StockTwits Firestream docs require Basic Authentication; "
                "v0 does not scrape unauthenticated web endpoints"
            ),
        )
    return source_result(
        source="stocktwits_firestream",
        market=market,
        query=symbol,
        status="credentials_present",
        items=[],
        observed_at=observed_at,
        error_reason=(
            "Credentials are present; add a bounded live stream smoke only after "
            "operator confirms account terms"
        ),
    )
```

- [ ] **Step 7: Run tests and verify they pass**

Run: `uv run pytest tests/services/social_sentiment_probe/test_sources.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/social_sentiment_probe/naver_openapi.py app/services/social_sentiment_probe/bluesky.py app/services/social_sentiment_probe/reddit.py app/services/social_sentiment_probe/stocktwits.py tests/services/social_sentiment_probe/test_sources.py
git commit -m "feat(rob-729): add free social source readers"
```

## Task 3: Optional X CDP Search Reader

**Files:**
- Create: `app/services/social_sentiment_probe/x_cdp.py`
- Modify: `tests/services/social_sentiment_probe/test_sources.py`

**Interfaces:**
- Consumes: `app.services.action_report.remote_debug_audit.cdp_client.CdpSession`
- Produces: `x_search_url(query: str) -> str`
- Produces: `parse_x_search_items(raw: Any) -> list[dict[str, Any]]`
- Produces: `fetch_x_search_cdp(query: str, market: str, cdp_session: CdpSession, limit: int = 10, now: datetime | None = None) -> dict[str, Any]`

- [ ] **Step 1: Add failing X CDP parser tests**

Append to `tests/services/social_sentiment_probe/test_sources.py`:

```python
from app.services.social_sentiment_probe.x_cdp import (
    fetch_x_search_cdp,
    parse_x_search_items,
    x_search_url,
)


class FakeCdp:
    def __init__(self, raw: object) -> None:
        self.raw = raw
        self.calls: list[dict[str, object]] = []

    async def fetch_rendered(
        self,
        url: str,
        js: str,
        *,
        timeout_s: float,
        ready_js: str | None = None,
    ) -> object:
        self.calls.append(
            {"url": url, "js": js, "timeout_s": timeout_s, "ready_js": ready_js}
        )
        return self.raw


def test_x_search_url_uses_latest_search_page() -> None:
    assert x_search_url("AAPL earnings") == "https://x.com/search?q=AAPL%20earnings&src=typed_query&f=live"


def test_parse_x_search_items_accepts_json_string() -> None:
    raw = '[{"url":"https://x.com/u/status/1","author":"u","published_at":"2026-07-06T00:00:00Z","text":"AAPL move"}]'
    items = parse_x_search_items(raw)
    assert items == [
        {
            "title": None,
            "url": "https://x.com/u/status/1",
            "author": "u",
            "published_at": "2026-07-06T00:00:00Z",
            "text_preview": "AAPL move",
            "metrics": {},
        }
    ]


@pytest.mark.asyncio
async def test_fetch_x_search_cdp_uses_cdp_session() -> None:
    cdp = FakeCdp('[{"url":"https://x.com/u/status/1","text":"NVDA"}]')
    out = await fetch_x_search_cdp("NVDA", "us", cdp, now=_now())
    assert out["status"] == "ok"
    assert out["items"][0]["text_preview"] == "NVDA"
    assert cdp.calls[0]["url"].startswith("https://x.com/search?")
    assert cdp.calls[0]["ready_js"] is not None
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/services/social_sentiment_probe/test_sources.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.social_sentiment_probe.x_cdp'`.

- [ ] **Step 3: Implement the X CDP reader**

Create `app/services/social_sentiment_probe/x_cdp.py`:

```python
from __future__ import annotations

import datetime as dt
import json
from typing import Any
from urllib.parse import quote

from app.services.action_report.remote_debug_audit.cdp_client import CdpSession
from app.services.social_sentiment_probe.models import source_result, truncate_preview


def x_search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"


X_SEARCH_EXTRACT_JS = """
(function(){
  const rows = Array.from(document.querySelectorAll('article')).slice(0, 10);
  return JSON.stringify(rows.map((article) => {
    const links = Array.from(article.querySelectorAll('a')).map((a) => a.href);
    const statusUrl = links.find((href) => /\\/status\\//.test(href)) || null;
    const time = article.querySelector('time');
    const textNode = article.querySelector('[data-testid="tweetText"]');
    const userLink = links.find((href) => /^https:\\/\\/x\\.com\\/[^/]+$/.test(href));
    return {
      url: statusUrl,
      author: userLink ? userLink.replace('https://x.com/', '') : null,
      published_at: time ? time.getAttribute('datetime') : null,
      text: textNode ? textNode.innerText : null
    };
  }));
})()
"""

X_SEARCH_READY_JS = """
(function(){
  return document.querySelectorAll('article').length > 0 ||
    document.body.innerText.includes('Log in') ||
    document.body.innerText.includes('Something went wrong');
})()
"""


def parse_x_search_items(raw: Any) -> list[dict[str, Any]]:
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    items: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "title": None,
                "url": row.get("url"),
                "author": row.get("author"),
                "published_at": row.get("published_at"),
                "text_preview": truncate_preview(row.get("text")),
                "metrics": {},
            }
        )
    return items


async def fetch_x_search_cdp(
    query: str,
    market: str,
    cdp_session: CdpSession,
    *,
    limit: int = 10,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    try:
        raw = await cdp_session.fetch_rendered(
            x_search_url(query),
            X_SEARCH_EXTRACT_JS.replace("slice(0, 10)", f"slice(0, {max(1, min(limit, 20))})"),
            timeout_s=15.0,
            ready_js=X_SEARCH_READY_JS,
        )
    except Exception as exc:
        return source_result(
            source="x_cdp",
            market=market,
            query=query,
            status="unavailable",
            items=[],
            observed_at=observed_at,
            error_reason=f"{type(exc).__name__}: {exc}",
        )
    return source_result(
        source="x_cdp",
        market=market,
        query=query,
        status="ok",
        items=parse_x_search_items(raw),
        observed_at=observed_at,
    )
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/services/social_sentiment_probe/test_sources.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/social_sentiment_probe/x_cdp.py tests/services/social_sentiment_probe/test_sources.py
git commit -m "feat(rob-729): add optional x cdp search reader"
```

## Task 4: Operator CLI Orchestration

**Files:**
- Create: `scripts/free_social_sources_probe.py`
- Create: `tests/services/social_sentiment_probe/test_cli.py`

**Interfaces:**
- Consumes: all source readers from Tasks 2 and 3.
- Produces: `default_sources_for_market(market: str) -> tuple[str, ...]`
- Produces: `build_parser() -> argparse.ArgumentParser`
- Produces: `run_probe(args: argparse.Namespace, now: datetime | None = None, source_runner: SourceRunner | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write the failing CLI tests**

Create `tests/services/social_sentiment_probe/test_cli.py`:

```python
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any

import pytest

from scripts import free_social_sources_probe as cli


def _now() -> dt.datetime:
    return dt.datetime(2026, 7, 6, 1, 2, 3, tzinfo=dt.UTC)


def test_default_sources_are_market_specific() -> None:
    assert cli.default_sources_for_market("kr") == (
        "naver_news",
        "naver_blog",
        "naver_cafe",
        "naver_discussion",
        "bluesky",
    )
    assert cli.default_sources_for_market("us") == ("reddit", "bluesky", "stocktwits")
    assert cli.default_sources_for_market("crypto") == ("reddit", "bluesky")


def test_parser_accepts_comma_separated_sources() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--market", "us", "--symbol", "AAPL", "--sources", "reddit,bluesky"]
    )
    assert args.market == "us"
    assert args.symbol == "AAPL"
    assert args.sources == "reddit,bluesky"


@pytest.mark.asyncio
async def test_run_probe_builds_social_sentiment_envelope() -> None:
    async def fake_runner(
        source: str,
        *,
        market: str,
        symbol: str,
        query: str,
        limit: int,
        include_x_cdp: bool,
        now: dt.datetime,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "market": market,
            "query": query,
            "status": "ok",
            "observed_at": now.isoformat(),
            "item_count": 1,
            "items": [{"title": f"{source}:{symbol}"}],
        }

    args = argparse.Namespace(
        market="us",
        symbol="AAPL",
        query=None,
        sources="reddit,bluesky",
        limit=5,
        include_x_cdp=False,
    )
    out = await cli.run_probe(args, now=_now(), source_runner=fake_runner)
    assert out["source"] == "free_social_sources_v0"
    assert out["query"] == "AAPL"
    assert out["summary"]["ok_source_count"] == 2
    assert [src["source"] for src in out["sources"]] == ["reddit", "bluesky"]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/services/social_sentiment_probe/test_cli.py -v`

Expected: FAIL with `ImportError: cannot import name 'free_social_sources_probe' from 'scripts'`.

- [ ] **Step 3: Implement CLI**

Create `scripts/free_social_sources_probe.py`:

```python
"""ROB-729 operator CLI for free social/opinion source sampling.

Prints one JSON evidence envelope. No DB writes, no orders, no broker calls.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from app.mcp_server.tooling.fundamentals._retail_sentiment import (
    handle_get_retail_sentiment,
)
from app.services.action_report.remote_debug_audit.cdp_client import CdpClient
from app.services.social_sentiment_probe.bluesky import fetch_bluesky_posts
from app.services.social_sentiment_probe.models import (
    build_social_sentiment_evidence,
    source_result,
)
from app.services.social_sentiment_probe.naver_openapi import fetch_naver_openapi
from app.services.social_sentiment_probe.reddit import fetch_reddit_search
from app.services.social_sentiment_probe.stocktwits import probe_stocktwits_firestream
from app.services.social_sentiment_probe.x_cdp import fetch_x_search_cdp

SourceRunner = Callable[..., Awaitable[dict[str, Any]]]


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def default_sources_for_market(market: str) -> tuple[str, ...]:
    normalized = market.strip().lower()
    if normalized == "kr":
        return ("naver_news", "naver_blog", "naver_cafe", "naver_discussion", "bluesky")
    if normalized == "us":
        return ("reddit", "bluesky", "stocktwits")
    if normalized == "crypto":
        return ("reddit", "bluesky")
    raise ValueError("market must be one of: kr, us, crypto")


def _parse_sources(raw: str | None, market: str) -> tuple[str, ...]:
    if not raw:
        return default_sources_for_market(market)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


async def _run_source(
    source: str,
    *,
    market: str,
    symbol: str,
    query: str,
    limit: int,
    include_x_cdp: bool,
    now: dt.datetime,
) -> dict[str, Any]:
    if source == "naver_news":
        return await fetch_naver_openapi(
            "news",
            query,
            market,
            os.getenv("NAVER_CLIENT_ID"),
            os.getenv("NAVER_CLIENT_SECRET"),
            display=limit,
            now=now,
        )
    if source == "naver_blog":
        return await fetch_naver_openapi(
            "blog",
            query,
            market,
            os.getenv("NAVER_CLIENT_ID"),
            os.getenv("NAVER_CLIENT_SECRET"),
            display=limit,
            now=now,
        )
    if source == "naver_cafe":
        return await fetch_naver_openapi(
            "cafearticle",
            query,
            market,
            os.getenv("NAVER_CLIENT_ID"),
            os.getenv("NAVER_CLIENT_SECRET"),
            display=limit,
            now=now,
        )
    if source == "naver_discussion":
        if market != "kr":
            return source_result(
                source="naver_discussion",
                market=market,
                query=query,
                status="unsupported_market",
                items=[],
                observed_at=now,
                error_reason="Naver discussion aggregate signal supports KR only",
            )
        payload = await handle_get_retail_sentiment(symbol, market="kr")
        return source_result(
            source="naver_discussion",
            market=market,
            query=query,
            status=payload.get("status", "unavailable"),
            items=[payload] if payload.get("status") == "ok" else [],
            observed_at=now,
            error_reason=payload.get("note") or payload.get("error"),
        )
    if source == "reddit":
        return await fetch_reddit_search(
            query,
            market,
            os.getenv("REDDIT_CLIENT_ID"),
            os.getenv("REDDIT_CLIENT_SECRET"),
            os.getenv("REDDIT_USER_AGENT"),
            subreddits=("stocks", "wallstreetbets") if market == "us" else ("CryptoCurrency",),
            limit=limit,
            now=now,
        )
    if source == "bluesky":
        return await fetch_bluesky_posts(query, market, limit=limit, now=now)
    if source == "stocktwits":
        return probe_stocktwits_firestream(
            symbol,
            market,
            os.getenv("STOCKTWITS_FIRESTREAM_USERNAME"),
            os.getenv("STOCKTWITS_FIRESTREAM_PASSWORD"),
            now=now,
        )
    if source == "x_cdp":
        if not include_x_cdp:
            return source_result(
                source="x_cdp",
                market=market,
                query=query,
                status="disabled",
                items=[],
                observed_at=now,
                error_reason="pass --include-x-cdp to use the local Chrome session",
            )
        return await fetch_x_search_cdp(query, market, CdpClient(), limit=limit, now=now)
    return source_result(
        source=source,
        market=market,
        query=query,
        status="unknown_source",
        items=[],
        observed_at=now,
        error_reason=f"unknown source: {source}",
    )


async def run_probe(
    args: argparse.Namespace,
    *,
    now: dt.datetime | None = None,
    source_runner: SourceRunner | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    market = args.market.strip().lower()
    symbol = args.symbol.strip()
    query = (args.query or symbol).strip()
    runner = source_runner or _run_source
    results = []
    for source in _parse_sources(args.sources, market):
        results.append(
            await runner(
                source,
                market=market,
                symbol=symbol,
                query=query,
                limit=args.limit,
                include_x_cdp=args.include_x_cdp,
                now=observed_at,
            )
        )
    return build_social_sentiment_evidence(
        market=market,
        symbol=symbol,
        query=query,
        source_results=results,
        observed_at=observed_at,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Free social/opinion source probe (ROB-729, operator-only)"
    )
    parser.add_argument("--market", required=True, choices=["kr", "us", "crypto"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--query", default=None)
    parser.add_argument("--sources", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-x-cdp", action="store_true")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    _emit(await run_probe(args))
    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run CLI tests and verify they pass**

Run: `uv run pytest tests/services/social_sentiment_probe/test_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Run all ROB-729 unit tests**

Run: `uv run pytest tests/services/social_sentiment_probe -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/free_social_sources_probe.py tests/services/social_sentiment_probe/test_cli.py
git commit -m "feat(rob-729): add free social sources probe cli"
```

## Task 5: Runbook and Verification

**Files:**
- Create: `docs/runbooks/free-social-sources-v0.md`

**Interfaces:**
- Consumes: `scripts.free_social_sources_probe`
- Produces: Operator commands and live-smoke acceptance checks.

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/free-social-sources-v0.md`:

```markdown
# Free Social Sources v0 (ROB-729)

Operator-only read probe for free social/opinion sources. It prints one JSON object
intended to be copied into `evidence_snapshot["social_sentiment"]` during a manual
session.

## Safety

- No DB writes.
- No orders, order-intent, broker, watchlist, or scheduler calls.
- Social/opinion data is advisory evidence only.
- Naver discussion is aggregate-only and uses the existing gated handler.
- X CDP is disabled unless `--include-x-cdp` is passed.
- StockTwits Firestream is reported as `requires_credentials` unless the operator
  provides Firestream credentials; v0 does not scrape StockTwits web pages.

## Environment

```bash
export NAVER_CLIENT_ID=...
export NAVER_CLIENT_SECRET=...
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_USER_AGENT='script:auto_trader.rob729:v0.1 (by /u/<operator>)'
```

Optional:

```bash
export RETAIL_SENTIMENT_LIVE_ENABLED=true
export STOCKTWITS_FIRESTREAM_USERNAME=...
export STOCKTWITS_FIRESTREAM_PASSWORD=...
```

For X CDP, launch the local logged-in Chrome profile:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.hermes/chrome-toss-debug"
```

## Examples

KR:

```bash
uv run python -m scripts.free_social_sources_probe \
  --market kr \
  --symbol 005930 \
  --query 삼성전자 \
  --limit 5
```

US:

```bash
uv run python -m scripts.free_social_sources_probe \
  --market us \
  --symbol NVDA \
  --query NVDA \
  --sources reddit,bluesky,stocktwits \
  --limit 5
```

X CDP opt-in:

```bash
uv run python -m scripts.free_social_sources_probe \
  --market us \
  --symbol AAPL \
  --query "AAPL earnings" \
  --sources x_cdp \
  --include-x-cdp \
  --limit 5
```

## Acceptance

- The command exits `0`.
- The top-level JSON has `source="free_social_sources_v0"`.
- `advisory_only` is `true`.
- `cost_usd` is `0`.
- Missing credentials produce `status="missing_credentials"` or
  `status="requires_credentials"` source entries, not a process crash.
- No source item contains Naver discussion raw title, body, author, nickname, or
  comment text.
```

- [ ] **Step 2: Run targeted tests**

Run: `uv run pytest tests/services/social_sentiment_probe -v`

Expected: PASS.

- [ ] **Step 3: Run lint on touched Python files**

Run:

```bash
uv run ruff check app/services/social_sentiment_probe scripts/free_social_sources_probe.py tests/services/social_sentiment_probe
```

Expected: PASS.

- [ ] **Step 4: Run a no-credential smoke**

Run:

```bash
uv run python -m scripts.free_social_sources_probe --market us --symbol AAPL --sources reddit,bluesky,stocktwits --limit 1
```

Expected: JSON output with:

```json
{
  "source": "free_social_sources_v0",
  "advisory_only": true,
  "cost_usd": 0
}
```

The `reddit` source may be `missing_credentials` without env vars. The `stocktwits_firestream` source must be `requires_credentials` without Firestream env vars. `bluesky` is allowed to be `ok` or `unavailable` depending on current network availability.

- [ ] **Step 5: Commit**

```bash
git add docs/runbooks/free-social-sources-v0.md
git commit -m "docs(rob-729): document free social sources v0 probe"
```

## Self-Review Checklist

- ROB-729 no paid X API decision is covered by the absence of X official API calls and the optional `x_cdp` source.
- KR sources are covered by Naver OpenAPI `news/blog/cafearticle`, existing Naver discussion aggregate, and optional Bluesky query.
- US and crypto sources are covered by Reddit and Bluesky.
- StockTwits is covered as a condition probe and does not imply free unauthenticated access.
- Governance is covered by `advisory_only=true`, zero writes, no order flow, and aggregate-only Naver discussion.
- v1 persistent DB pipeline is not included.
- MCP public contracts are unchanged, so `app/mcp_server/README.md` is not touched.
