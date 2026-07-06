from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.services.social_sentiment_probe.bluesky import fetch_bluesky_posts
from app.services.social_sentiment_probe.naver_openapi import fetch_naver_openapi
from app.services.social_sentiment_probe.reddit import fetch_reddit_search
from app.services.social_sentiment_probe.stocktwits import probe_stocktwits_firestream
from app.services.social_sentiment_probe.x_cdp import (
    fetch_x_search_cdp,
    parse_x_search_items,
    x_search_url,
)


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
    out = await fetch_naver_openapi("news", "삼성전자", "kr", None, None, now=_now())
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
async def test_naver_openapi_ignores_malformed_text_fields() -> None:
    async def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"items": [{"title": 123, "description": ["bad"]}]})

    out = await fetch_naver_openapi(
        "news", "삼성전자", "kr", "cid", "secret", now=_now(), http_get=fake_get
    )
    assert out["status"] == "ok"
    assert out["items"][0]["title"] is None
    assert out["items"][0]["text_preview"] is None


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
                        "record": {
                            "text": "AAPL breakout",
                            "createdAt": "2026-07-06T00:00:00Z",
                        },
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
async def test_bluesky_ignores_malformed_text_fields() -> None:
    async def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"posts": [{"record": {"text": ["bad"]}, "author": {}}]})

    out = await fetch_bluesky_posts("AAPL", "us", now=_now(), http_get=fake_get)
    assert out["status"] == "ok"
    assert out["items"][0]["text_preview"] is None


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


@pytest.mark.asyncio
async def test_reddit_ignores_malformed_text_fields() -> None:
    async def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"access_token": "tok", "token_type": "bearer"})

    async def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "NVDA earnings",
                                "selftext": ["bad"],
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
    assert out["items"][0]["text_preview"] is None


def test_stocktwits_without_credentials_reports_requires_credentials() -> None:
    out = probe_stocktwits_firestream("AAPL", "us", None, None, now=_now())
    assert out["status"] == "requires_credentials"
    assert out["source"] == "stocktwits_firestream"
    assert out["items"] == []


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
    assert (
        x_search_url("AAPL earnings")
        == "https://x.com/search?q=AAPL%20earnings&src=typed_query&f=live"
    )


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
