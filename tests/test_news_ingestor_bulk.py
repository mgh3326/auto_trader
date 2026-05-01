"""Tests for news-ingestor bulk ingestion and readiness (ROB-46)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_test_app() -> FastAPI:
    from app.routers.news_analysis import router

    app = FastAPI()
    app.include_router(router)
    return app


def _sample_payload() -> dict:
    return {
        "ingestion_run": {
            "run_uuid": "run-rob-46",
            "market": "kr",
            "feed_set": "kr-core",
            "started_at": "2026-04-30T01:42:31+00:00",
            "finished_at": "2026-04-30T01:42:37+00:00",
            "source_counts": {
                "browser_naver_mainnews": 1,
                "browser_naver_research": 1,
            },
        },
        "articles": [
            {
                "fingerprint": "fp-1",
                "market": "kr",
                "source": "browser_naver_mainnews",
                "title": "시장 뉴스",
                "url": "https://n.news.naver.com/mnews/article/001/1",
                "canonical_url": "https://n.news.naver.com/mnews/article/001/1",
                "publisher": "연합뉴스",
                "published_at": "2026-04-30T10:32:00+09:00",
                "summary": "요약",
                "raw": {"row_index": 0},
            }
        ],
    }


class TestNewsIngestorSchemas:
    def test_news_ingestor_bulk_schema_maps_payload(self):
        from app.schemas.news import NewsBulkIngestRequest

        request = NewsBulkIngestRequest.model_validate(_sample_payload())

        assert request.ingestion_run.run_uuid == "run-rob-46"
        assert request.ingestion_run.market == "kr"
        assert request.articles[0].market == "kr"
        assert request.articles[0].feed_source == "browser_naver_mainnews"
        assert request.articles[0].source == "연합뉴스"
        assert request.articles[0].url == "https://n.news.naver.com/mnews/article/001/1"
        assert request.articles[0].keywords == [
            "fingerprint:fp-1",
            "canonical_url:https://n.news.naver.com/mnews/article/001/1",
        ]

    def test_blank_canonical_url_does_not_override_valid_url(self):
        from app.schemas.news import NewsBulkIngestRequest

        payload = _sample_payload()
        payload["articles"][0]["canonical_url"] = "   "
        request = NewsBulkIngestRequest.model_validate(payload)

        assert request.articles[0].url == "https://n.news.naver.com/mnews/article/001/1"
        assert request.articles[0].canonical_url is None
        assert request.articles[0].keywords == ["fingerprint:fp-1"]

    def test_news_ingestion_run_model_has_required_columns(self):
        from app.models.news import NewsIngestionRun

        run = NewsIngestionRun(
            run_uuid="run-rob-46",
            market="kr",
            feed_set="kr-core",
            started_at=datetime(2026, 4, 30, 1, 42, 31),
            finished_at=datetime(2026, 4, 30, 1, 42, 37),
            status="success",
            source_counts={"browser_naver_mainnews": 20},
            inserted_count=12,
            skipped_count=39,
        )

        assert run.run_uuid == "run-rob-46"
        assert run.source_counts == {"browser_naver_mainnews": 20}
        assert run.inserted_count == 12
        assert run.skipped_count == 39

    def test_news_article_model_has_market_scope(self):
        from app.models.news import NewsArticle

        article = NewsArticle(
            url="https://cointelegraph.com/news/example",
            title="Bitcoin ETF update",
            market="crypto",
            feed_source="rss_cointelegraph",
            scraped_at=datetime(2026, 5, 1, 5, 0, 0),
            created_at=datetime(2026, 5, 1, 5, 0, 0),
        )

        assert article.market == "crypto"

    def test_ingestor_article_values_include_market_scope(self):
        from app.schemas.news import NewsBulkIngestRequest
        from app.services.llm_news_service import _article_values_from_ingestor_payload

        payload = _sample_payload()
        payload["ingestion_run"]["market"] = "crypto"
        payload["ingestion_run"]["feed_set"] = "crypto-core"
        payload["articles"][0]["market"] = "crypto"
        payload["articles"][0]["source"] = "rss_cointelegraph"
        payload["articles"][0]["publisher"] = "Cointelegraph"

        request = NewsBulkIngestRequest.model_validate(payload)
        values = _article_values_from_ingestor_payload(request.articles[0])

        assert values["market"] == "crypto"
        assert values["feed_source"] == "rss_cointelegraph"


class TestNewsIngestorRouter:
    def test_new_bulk_ingest_endpoint_exists_without_breaking_legacy_bulk(self):
        app = _make_test_app()
        routes = {route.path for route in app.routes}

        assert "/api/v1/news/bulk" in routes
        assert "/api/v1/news/ingest/bulk" in routes
        assert "/api/v1/news/readiness" in routes

    def test_bulk_ingest_endpoint_delegates_to_service(self):
        app = _make_test_app()
        client = TestClient(app)

        with patch(
            "app.routers.news_analysis.ingest_news_ingestor_bulk",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    success=True,
                    run_uuid="run-rob-46",
                    inserted_count=1,
                    skipped_count=0,
                    skipped_urls=[],
                )
            ),
        ) as mock_ingest:
            response = client.post("/api/v1/news/ingest/bulk", json=_sample_payload())

        assert response.status_code == 201
        assert response.json()["inserted_count"] == 1
        mock_ingest.assert_awaited_once()

    def test_list_news_endpoint_accepts_market_filter(self):
        app = _make_test_app()
        client = TestClient(app)

        with patch(
            "app.routers.news_analysis.get_news_articles",
            new=AsyncMock(return_value=([], 0)),
        ) as mock_get_news:
            response = client.get("/api/v1/news?market=crypto&limit=5")

        assert response.status_code == 200
        mock_get_news.assert_awaited_once()
        assert mock_get_news.await_args.kwargs["market"] == "crypto"

    def test_readiness_endpoint_returns_service_result(self):
        app = _make_test_app()
        client = TestClient(app)
        generated_at = datetime(2026, 4, 30, 10, 0, tzinfo=UTC)

        with patch(
            "app.routers.news_analysis.get_news_readiness",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    market="kr",
                    is_ready=True,
                    is_stale=False,
                    latest_run_uuid="run-rob-46",
                    latest_status="success",
                    latest_finished_at=generated_at,
                    latest_article_published_at=generated_at,
                    source_counts={"browser_naver_mainnews": 20},
                    warnings=[],
                    max_age_minutes=180,
                )
            ),
        ):
            response = client.get("/api/v1/news/readiness?market=kr")

        assert response.status_code == 200
        assert response.json()["is_ready"] is True
        assert response.json()["latest_run_uuid"] == "run-rob-46"


class TestNewsReadinessPreopenIntegration:
    def test_unfinished_latest_run_is_not_ready(self):
        from app.models.news import NewsIngestionRun
        from app.services.llm_news_service import _news_readiness_payload

        run = NewsIngestionRun(
            run_uuid="unfinished",
            market="kr",
            feed_set="kr-core",
            started_at=datetime.now(UTC),
            finished_at=None,
            status="success",
            source_counts={"browser_naver_mainnews": 20},
            inserted_count=20,
            skipped_count=0,
            created_at=datetime.now(UTC),
        )

        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=datetime.now(UTC),
            max_age_minutes=180,
        )

        assert result.is_ready is False
        assert "news_run_unfinished" in result.warnings

    def test_empty_source_counts_are_not_ready(self):
        from app.models.news import NewsIngestionRun
        from app.services.llm_news_service import _news_readiness_payload

        run = NewsIngestionRun(
            run_uuid="empty-sources",
            market="kr",
            feed_set="kr-core",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            status="success",
            source_counts={},
            inserted_count=0,
            skipped_count=0,
            created_at=datetime.now(UTC),
        )

        result = _news_readiness_payload(
            market="kr",
            latest_run=run,
            latest_article_published_at=datetime.now(UTC),
            max_age_minutes=180,
        )

        assert result.is_ready is False
        assert "news_sources_empty" in result.warnings

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_preopen_dashboard_adds_news_stale_warning(self):
        from app.services import preopen_dashboard_service, research_run_service

        run = SimpleNamespace(
            id=1,
            run_uuid=uuid4(),
            user_id=7,
            market_scope="kr",
            stage="preopen",
            status="open",
            source_profile="roadmap",
            strategy_name="Morning scan",
            notes=None,
            market_brief={"summary": "Cautious"},
            source_freshness={"existing": {"ok": True}},
            source_warnings=[],
            advisory_links=[],
            generated_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            candidates=[],
            reconciliations=[],
        )
        readiness = SimpleNamespace(
            market="kr",
            is_ready=False,
            is_stale=True,
            latest_run_uuid="run-old",
            latest_status="success",
            latest_finished_at=datetime.now(UTC) - timedelta(hours=8),
            latest_article_published_at=datetime.now(UTC) - timedelta(hours=8),
            source_counts={"browser_naver_mainnews": 20},
            warnings=["news_stale"],
            max_age_minutes=180,
        )

        with (
            patch.object(
                research_run_service,
                "get_latest_research_run",
                new=AsyncMock(return_value=run),
            ),
            patch.object(
                preopen_dashboard_service,
                "_linked_sessions",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                preopen_dashboard_service,
                "get_news_readiness",
                new=AsyncMock(return_value=readiness),
            ),
        ):
            result = await preopen_dashboard_service.get_latest_preopen_dashboard(
                db=AsyncMock(),
                user_id=7,
                market_scope="kr",
            )

        assert "news_stale" in result.source_warnings
        assert result.source_freshness is not None
        assert result.source_freshness["news"]["is_stale"] is True
        assert result.source_freshness["news"]["latest_run_uuid"] == "run-old"
