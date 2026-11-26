"""ROB-162 Task 10: lock tvscreener bulk-ingest content field round-trip.

Verifies that:
  1. When `content` is provided in a NewsBulkIngestRequest, it persists to
     news_articles.article_content.
  2. When `content` is absent, news_articles.article_content is NULL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_ingest_dedupes_repeated_urls_and_preserves_richer_existing_row(
    db_session,
):
    from app.models.news import NewsArticle

    run_uuid = f"test-rob-162-repeat-url-{uuid4().hex}"
    payload = _unique_payload(run_uuid)
    article_payload = payload["articles"][0]
    payload["articles"] = [article_payload, dict(article_payload)]
    target_url = article_payload.get("canonical_url") or article_payload["url"]

    first_received = datetime(2026, 4, 30, 1, 0, tzinfo=UTC)
    published_at = datetime(2026, 4, 30, 1, 32, tzinfo=UTC)
    db_session.add(
        NewsArticle(
            url=target_url,
            title=article_payload["title"],
            source="stored publisher",
            author="stored author",
            article_content="stored body",
            summary="stored summary",
            market="kr",
            feed_source="stored feed",
            scraped_at=first_received,
            created_at=first_received,
            first_received_at=first_received,
            published_at=published_at,
            published_at_raw="2026-04-30T10:32:00+09:00",
            published_offset="+09:00",
            published_precision="second",
            provider_article_id="provider-article-7",
            source_document_ids={"dart_id": ["20260430000007"]},
        )
    )
    await db_session.commit()

    article_payload.update(
        {
            "title": article_payload["title"],
            "market": "us",
            "source": "incoming-feed",
            "publisher": None,
            "content": None,
            "summary": None,
            "published_at": "2026-04-30T00:00:00+00:00",
            "published_at_raw": "2026-04-30",
            "published_precision": "date",
            "raw": {},
        }
    )
    request = NewsBulkIngestRequest.model_validate(payload)
    response = await ingest_news_ingestor_bulk(request)

    assert response.success
    assert response.inserted_count == 0
    assert response.skipped_count == 2
    stored = (
        await db_session.execute(
            select(NewsArticle).where(NewsArticle.url == target_url)
        )
    ).scalar_one()
    assert stored.first_received_at == first_received
    assert stored.title == article_payload["title"]
    assert stored.source == "stored publisher"
    assert stored.author == "stored author"
    assert stored.article_content == "stored body"
    assert stored.summary == "stored summary"
    assert stored.market == "kr"
    assert stored.feed_source == "stored feed"
    assert stored.published_at == published_at
    assert stored.published_at_raw == "2026-04-30T10:32:00+09:00"
    assert stored.published_precision == "second"
    assert stored.revised_at is None
    assert stored.provider_article_id == "provider-article-7"
    assert stored.source_document_ids == {"dart_id": ["20260430000007"]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_news_ingestor_bulk_publication_time_change_marks_revision(db_session):
    payload = _unique_payload(f"test-news-timing-revision-{uuid4().hex}")
    article_payload = payload["articles"][0]
    target_url = article_payload.get("canonical_url") or article_payload["url"]
    first_received = datetime(2026, 4, 30, 1, 0, tzinfo=UTC)
    db_session.add(
        NewsArticle(
            url=target_url,
            title=article_payload["title"],
            source=article_payload.get("source") or article_payload.get("publisher"),
            article_content=article_payload.get("content"),
            summary=article_payload.get("summary"),
            market=article_payload["market"],
            feed_source="stored-feed",
            scraped_at=first_received,
            created_at=first_received,
            first_received_at=first_received,
            published_at=datetime(2026, 4, 29, 15, 0, tzinfo=UTC),
            published_at_raw="2026-04-30",
            published_precision="date",
        )
    )
    await db_session.commit()

    article_payload.update(
        {
            "published_at": "2026-04-29T15:32:00+00:00",
            "published_at_raw": "2026-04-30T00:32:00+09:00",
            "published_offset": "+09:00",
            "published_precision": "second",
        }
    )
    response = await ingest_news_ingestor_bulk(
        NewsBulkIngestRequest.model_validate(payload)
    )

    assert response.success
    stored = (
        await db_session.execute(
            select(NewsArticle).where(NewsArticle.url == target_url)
        )
    ).scalar_one()
    assert stored.first_received_at == first_received
    assert stored.published_at_raw == "2026-04-30T00:32:00+09:00"
    assert stored.published_at == datetime(2026, 4, 29, 15, 32, tzinfo=UTC)
    assert stored.published_offset == "+09:00"
    assert stored.published_precision == "second"
    assert stored.revised_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_ingest_reports_repeated_new_url_as_skipped(db_session):
    payload = _unique_payload(f"test-rob-162-new-repeat-{uuid4().hex}")
    article_payload = payload["articles"][0]
    payload["articles"] = [article_payload, dict(article_payload)]
    target_url = article_payload.get("canonical_url") or article_payload["url"]

    response = await ingest_news_ingestor_bulk(
        NewsBulkIngestRequest.model_validate(payload)
    )

    assert response.success
    assert response.inserted_count == 1
    assert response.skipped_count == 1
    assert response.skipped_urls == [article_payload["url"]]
    assert (
        await db_session.execute(
            select(NewsArticle.id).where(NewsArticle.url == target_url)
        )
    ).scalar_one()
