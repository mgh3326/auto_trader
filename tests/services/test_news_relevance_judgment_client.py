"""ROB-506 — NewsRelevanceJudgmentClient contract tests (httpx MockTransport)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.services.news_relevance_judgment_client import (
    NewsRelevanceJudgmentClient,
)

_PENDING = [
    {
        "article_id": 101,
        "market": "kr",
        "symbol": "035420",
        "url": "https://x/a",
        "title": "네이버 신규 투자",
        "source": "매일경제",
        "published_at": "2026-06-10T09:00:00",
        "first_seen_at": "2026-06-10T09:05:00",
        "hints": None,
    }
]

_JUDGMENT = {
    "article_id": 101,
    "market": "kr",
    "symbol": "035420",
    "relationship": "direct",
    "relevance": "high",
    "price_relevance": "catalyst",
    "score": 0.9,
    "reason": "직접 보도",
    "judged_by": "hermes",
}


def _client(handler, **kwargs) -> NewsRelevanceJudgmentClient:
    return NewsRelevanceJudgmentClient(
        webhook_url="https://hermes.test/hooks/news-relevance-judgment",
        token="sekrit-token",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_judgments_response_is_judged() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"judgments": [_JUDGMENT]})

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()

    assert result.status == "judged"
    assert len(result.judgments) == 1
    assert result.judgments[0].article_id == 101
    assert captured["headers"]["authorization"] == "Bearer sekrit-token"
    assert captured["body"]["kind"] == "news_relevance_judgment_request"
    assert captured["body"]["pending"][0]["article_id"] == 101


@pytest.mark.unit
@pytest.mark.asyncio
async def test_accepted_without_judgments_is_dispatched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "accepted"})

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "dispatched"
    assert result.judgments == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_2xx_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "failed"
    assert result.http_status == 503
    assert result.reason == "http_503"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_network_error_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "failed"
    assert result.reason == "request_failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_judgment_items_are_counted_not_applied() -> None:
    bad = {**_JUDGMENT, "relevance": "ultra"}  # invalid enum

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"judgments": [_JUDGMENT, bad]})

    client = _client(handler)
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "judged"
    assert len(result.judgments) == 1
    assert result.invalid_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfigured_url_is_skipped_without_http() -> None:
    client = NewsRelevanceJudgmentClient(webhook_url="", token="")
    result = await client.request_judgments(
        market="kr", symbol="035420", pending=_PENDING
    )
    await client.close()
    assert result.status == "skipped"
    assert result.reason == "webhook_url_not_configured"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_never_appears_in_logs_or_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    client = _client(handler)
    with caplog.at_level(logging.DEBUG):
        result = await client.request_judgments(
            market="kr", symbol="035420", pending=_PENDING
        )
    await client.close()
    assert "sekrit-token" not in caplog.text
    assert "sekrit-token" not in repr(result)
