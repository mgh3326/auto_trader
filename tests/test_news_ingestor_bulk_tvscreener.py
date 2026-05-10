"""ROB-161: lock the tvscreener bulk-ingest contract from news-ingestor."""

from __future__ import annotations

import json
from pathlib import Path

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


class TestTvscreenerServiceContract:
    @pytest.mark.integration
    def test_bulk_ingest_round_trip_persists_articles_and_related_symbols(self):
        from sqlalchemy import select
        from app.models.news import (
            NewsArticle,
            NewsArticleRelatedSymbol,
            NewsIngestionRun,
        )
        from app.core.db import AsyncSessionLocal

        client = TestClient(_make_test_app())
        payload = _load_fixture()

        response = client.post("/api/v1/news/ingest/bulk", json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["success"] is True
        assert body["inserted_count"] == 5

        async def _load_persisted():
            async with AsyncSessionLocal() as db:
                articles = (
                    (await db.execute(select(NewsArticle).order_by(NewsArticle.id)))
                    .scalars()
                    .all()
                )
                relations = (
                    (
                        await db.execute(
                            select(NewsArticleRelatedSymbol).order_by(
                                NewsArticleRelatedSymbol.article_id,
                                NewsArticleRelatedSymbol.symbol,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                run = (
                    (await db.execute(select(NewsIngestionRun)))
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
        assert run is not None and run.inserted_count == 5

    @pytest.mark.integration
    def test_bulk_ingest_is_idempotent_by_run_uuid(self):
        client = TestClient(_make_test_app())
        payload = _load_fixture()

        first = client.post("/api/v1/news/ingest/bulk", json=payload).json()
        second = client.post("/api/v1/news/ingest/bulk", json=payload).json()

        assert first["run_uuid"] == second["run_uuid"]
        assert second["inserted_count"] == first["inserted_count"]
        assert second["skipped_count"] == 0


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
