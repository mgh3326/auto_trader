"""ROB-161: lock the tvscreener bulk-ingest contract from news-ingestor."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "news_ingestor"
    / "tvscreener_bulk_ingest_sample.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _unique_payload() -> dict:
    payload = _load_fixture()
    suffix = uuid4().hex
    payload["ingestion_run"]["run_uuid"] = f"rob161-contract-{suffix}"
    for index, article in enumerate(payload["articles"]):
        article["url"] = f"{article['url']}?test_run={suffix}&i={index}"
        article["canonical_url"] = article["url"]
        article["fingerprint"] = f"{article['fingerprint']}-{suffix}-{index}"
    return payload


@pytest.fixture(scope="module")
def news_schema_for_tvscreener_contract_tests():
    """Ensure ROB-46/ROB-161 news tables exist for full CI integration runs."""

    async def _ensure_schema() -> None:
        from sqlalchemy import text

        # Import model modules before create_all so their tables are registered.
        from app.core.db import engine
        from app.models.news import (
            NewsAnalysisResult,
            NewsArticle,
            NewsArticleRelatedSymbol,
            NewsIngestionRun,
        )
        from app.models.trading import User

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: NewsArticle.metadata.create_all(
                    sync_conn,
                    tables=[
                        User.__table__,
                        NewsArticle.__table__,
                        NewsArticleRelatedSymbol.__table__,
                        NewsIngestionRun.__table__,
                        NewsAnalysisResult.__table__,
                    ],
                )
            )
            # Local dev databases may predate the ROB-46 news market migration;
            # keep this test fixture idempotent without requiring destructive reset.
            await conn.execute(
                text(
                    "ALTER TABLE news_articles "
                    "ADD COLUMN IF NOT EXISTS market VARCHAR(20) NOT NULL DEFAULT 'kr'"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_news_articles_market "
                    "ON news_articles (market)"
                )
            )

    asyncio.run(_ensure_schema())


@pytest.mark.unit
class TestTvscreenerSchemaContract:
    def test_news_bulk_ingest_request_accepts_tvscreener_payload(self):
        from app.schemas.news import NewsBulkIngestRequest

        request = NewsBulkIngestRequest.model_validate(_load_fixture())

        assert request.ingestion_run.feed_set == "us-tvscreener"
        feed_sources = sorted({a.feed_source for a in request.articles})
        assert all(fs.startswith("http_tvscreener_news_") for fs in feed_sources)
        # publisher → source mapping per app/schemas/news.py
        assert {a.source for a in request.articles} >= {
            "Dow Jones Newswires",
            "Reuters",
            "CoinDesk",
        }

    def test_news_bulk_ingest_request_preserves_raw_metadata(self):
        from app.schemas.news import NewsBulkIngestRequest

        request = NewsBulkIngestRequest.model_validate(_load_fixture())
        first = request.articles[0]

        assert "tv_id" in first.raw
        assert first.raw["tv_related_symbols"]  # list of "PREFIX:SYMBOL"
        assert isinstance(first.raw.get("stock_candidates", []), list)


def _make_test_app() -> FastAPI:
    from app.routers.news_analysis import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.usefixtures("news_schema_for_tvscreener_contract_tests")
class TestTvscreenerServiceContract:
    @pytest.mark.integration
    def test_bulk_ingest_round_trip_persists_articles_and_related_symbols(self):
        from sqlalchemy import select

        from app.core.db import AsyncSessionLocal
        from app.models.news import (
            NewsArticle,
            NewsArticleRelatedSymbol,
            NewsIngestionRun,
        )

        client = TestClient(_make_test_app())
        payload = _unique_payload()

        response = client.post("/api/v1/news/ingest/bulk", json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True
        assert body["inserted_count"] == 5

        async def _load_persisted():
            async with AsyncSessionLocal() as db:
                urls = [article["url"] for article in payload["articles"]]
                articles = (
                    (
                        await db.execute(
                            select(NewsArticle)
                            .where(NewsArticle.url.in_(urls))
                            .order_by(NewsArticle.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                article_ids = [article.id for article in articles]
                relations = (
                    (
                        await db.execute(
                            select(NewsArticleRelatedSymbol)
                            .where(NewsArticleRelatedSymbol.article_id.in_(article_ids))
                            .order_by(
                                NewsArticleRelatedSymbol.article_id,
                                NewsArticleRelatedSymbol.symbol,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                run = (
                    (
                        await db.execute(
                            select(NewsIngestionRun).where(
                                NewsIngestionRun.run_uuid
                                == payload["ingestion_run"]["run_uuid"]
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                return articles, relations, run

        import asyncio

        articles, relations, run = asyncio.run(_load_persisted())

        feed_sources = {a.feed_source for a in articles}
        assert feed_sources <= {
            "http_tvscreener_news_kr",
            "http_tvscreener_news_us",
            "http_tvscreener_news_crypto",
        }
        rel_keys = {(r.market, r.symbol) for r in relations}
        assert ("kr", "005930") in rel_keys
        assert ("us", "AAPL") in rel_keys
        assert ("crypto", "BTCUSDT") in rel_keys
        assert not any(market == "uk" for market, _ in rel_keys)
        assert {r.source for r in relations} == {"tv_related_symbol"}
        assert run is not None and run.inserted_count == 5

    @pytest.mark.integration
    def test_bulk_ingest_is_idempotent_by_run_uuid(self):
        client = TestClient(_make_test_app())
        payload = _unique_payload()

        first = client.post("/api/v1/news/ingest/bulk", json=payload).json()
        second = client.post("/api/v1/news/ingest/bulk", json=payload).json()

        assert first["run_uuid"] == second["run_uuid"]
        assert second["inserted_count"] == first["inserted_count"]
        assert second["skipped_count"] == 0


@pytest.mark.unit
class TestParseTradingviewSymbol:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("KRX:005930", ("kr", "005930")),
            ("KOSPI:005930", ("kr", "005930")),
            ("KOSDAQ:035420", ("kr", "035420")),
            ("KRX:5930", ("kr", "005930")),  # zero-pad short codes
            ("NASDAQ:AAPL", ("us", "AAPL")),
            ("NYSE:GME", ("us", "GME")),
            ("AMEX:SPY", ("us", "SPY")),
            ("BINANCE:BTCUSDT", ("crypto", "BTCUSDT")),
            ("BITSTAMP:BTCUSD", ("crypto", "BTCUSD")),
            ("COINBASE:ETHUSD", ("crypto", "ETHUSD")),
            ("UPBIT:BTCKRW", ("crypto", "BTCKRW")),
            ("nasdaq:aapl", ("us", "AAPL")),  # case-insensitive
            ("  NASDAQ:AAPL ", ("us", "AAPL")),  # whitespace-tolerant
        ],
    )
    def test_supported_prefixes_round_trip(self, token, expected):
        from app.services.news_payload_normalizer import _parse_tradingview_symbol

        assert _parse_tradingview_symbol(token) == expected

    @pytest.mark.parametrize(
        "token",
        [
            "LSE:VOD",
            "TSE:7203",
            "FX:EURUSD",
            "FX_IDC:EURUSD",
            "OANDA:USDJPY",
            "TVC:GOLD",
            "INDEX:SPX",
            "SP:SPX",
            "NASDAQ:",  # missing symbol
            ":AAPL",  # missing prefix
            "AAPL",  # no colon
            "",
            None,
            "https://example.com/",  # url-like
            "canonical_url:https://...",
        ],
    )
    def test_unsupported_returns_none(self, token):
        from app.services.news_payload_normalizer import _parse_tradingview_symbol

        assert _parse_tradingview_symbol(token) is None


@pytest.mark.unit
class TestTvscreenerCandidateFallback:
    def test_falls_back_to_tv_related_symbols_when_stock_candidates_missing(self):
        from app.schemas.news import NewsBulkIngestRequest
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        # Minimal valid payload, raw with ONLY tv_related_symbols (no stock_candidates).
        payload = {
            "ingestion_run": {
                "run_uuid": "rob-161-fallback",
                "market": "us",
                "feed_set": "us-tvscreener",
                "started_at": "2026-05-10T00:00:00+00:00",
                "finished_at": "2026-05-10T00:01:00+00:00",
                "source_counts": {"http_tvscreener_news_us": 1},
            },
            "articles": [
                {
                    "fingerprint": "fp-fallback-1",
                    "market": "us",
                    "source": "http_tvscreener_news_us",
                    "title": "Mixed-market story",
                    "url": "https://example.com/news/mixed",
                    "canonical_url": "https://example.com/news/mixed",
                    "publisher": "Reuters",
                    "published_at": "2026-05-10T00:00:00+00:00",
                    "raw": {
                        "tv_related_symbols": [
                            "KRX:005930",
                            "NASDAQ:AAPL",
                            "BINANCE:BTCUSDT",
                            "LSE:VOD",  # unsupported — must be skipped
                            "FX:EURUSD",  # unsupported — must be skipped
                        ],
                    },
                }
            ],
        }
        request = NewsBulkIngestRequest.model_validate(payload)

        rows = _related_symbol_values_from_ingestor_payload(
            article_id=42, article_data=request.articles[0]
        )

        keys = sorted((r["market"], r["symbol"]) for r in rows)
        assert keys == [
            ("crypto", "BTCUSDT"),
            ("kr", "005930"),
            ("us", "AAPL"),
        ]
        # source attribution preserves tv_related_symbols fallback provenance.
        assert {r["source"] for r in rows} == {"tv_related_symbol"}

    def test_stock_candidates_take_precedence_over_tv_related_symbols(self):
        from app.schemas.news import NewsBulkIngestRequest
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        payload = {
            "ingestion_run": {
                "run_uuid": "rob-161-precedence",
                "market": "us",
                "feed_set": "us-tvscreener",
                "started_at": "2026-05-10T00:00:00+00:00",
                "finished_at": "2026-05-10T00:01:00+00:00",
                "source_counts": {"http_tvscreener_news_us": 1},
            },
            "articles": [
                {
                    "fingerprint": "fp-precedence",
                    "market": "us",
                    "source": "http_tvscreener_news_us",
                    "title": "Authoritative candidates",
                    "url": "https://example.com/news/precedence",
                    "canonical_url": "https://example.com/news/precedence",
                    "publisher": "Reuters",
                    "published_at": "2026-05-10T00:00:00+00:00",
                    "raw": {
                        "stock_candidates": [
                            {
                                "market": "us",
                                "symbol": "AAPL",
                                "source": "tv_related_symbol",
                                "match_type": "tv_related",
                                "confidence": 0.9,
                            }
                        ],
                        # These should be ignored once stock_candidates exists.
                        "tv_related_symbols": ["KRX:005930", "BINANCE:BTCUSDT"],
                    },
                }
            ],
        }
        request = NewsBulkIngestRequest.model_validate(payload)

        rows = _related_symbol_values_from_ingestor_payload(
            article_id=99, article_data=request.articles[0]
        )

        assert [(r["market"], r["symbol"]) for r in rows] == [("us", "AAPL")]
