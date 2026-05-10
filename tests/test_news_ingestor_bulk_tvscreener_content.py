"""ROB-162 Task 10: lock tvscreener bulk-ingest content field round-trip.

Verifies that:
  1. When `content` is provided in a NewsBulkIngestRequest, it persists to
     news_articles.article_content.
  2. When `content` is absent, news_articles.article_content is NULL.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.news import NewsArticle
from app.schemas.news import NewsBulkIngestRequest
from app.services.llm_news_service import ingest_news_ingestor_bulk

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "news_ingestor"
    / "tvscreener_bulk_ingest_sample.json"
)


def _unique_payload(run_uuid: str) -> dict:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    suffix = uuid4().hex[:12]
    payload["ingestion_run"]["run_uuid"] = run_uuid
    for index, article in enumerate(payload["articles"]):
        article["url"] = f"{article['url']}?run={suffix}&i={index}"
        article["canonical_url"] = article["url"]
        # Replace fingerprint entirely to stay within 128-char limit.
        article["fingerprint"] = f"rob162-{suffix}-{index}"
    return payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tvscreener_bulk_ingest_with_content_persists_article_content(
    db_session,
):
    run_uuid = f"test-rob-162-content-roundtrip-{uuid4().hex}"
    payload = _unique_payload(run_uuid)
    payload["articles"][0]["content"] = (
        "Quality-gated body text from tvscreener enrichment."
    )

    request = NewsBulkIngestRequest.model_validate(payload)
    response = await ingest_news_ingestor_bulk(request)
    assert response.success
    assert response.inserted_count >= 1

    target_url = (
        payload["articles"][0].get("canonical_url") or payload["articles"][0]["url"]
    )
    result = await db_session.execute(
        select(NewsArticle.article_content).where(NewsArticle.url == target_url)
    )
    article_content = result.scalar_one()
    assert article_content == ("Quality-gated body text from tvscreener enrichment.")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tvscreener_bulk_ingest_without_content_leaves_article_content_null(
    db_session,
):
    run_uuid = f"test-rob-162-content-null-{uuid4().hex}"
    payload = _unique_payload(run_uuid)
    for article in payload["articles"]:
        article.pop("content", None)

    request = NewsBulkIngestRequest.model_validate(payload)
    response = await ingest_news_ingestor_bulk(request)
    assert response.success

    target_url = (
        payload["articles"][0].get("canonical_url") or payload["articles"][0]["url"]
    )
    result = await db_session.execute(
        select(NewsArticle.article_content).where(NewsArticle.url == target_url)
    )
    assert result.scalar_one() is None
