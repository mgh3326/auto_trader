"""ROB-916: KR symbol-universe name matcher wiring + backfill script tests.

Covers the news→symbol mapping repair: the news-ingestor bulk endpoint gets a
supplementary deterministic match against `kr_symbol_universe` when the
upstream ingestor's own candidate metadata misses an explicit company-name
mention (the 한화오션/http_naver_stock_aggregate evidence case), and the
`scripts/backfill_news_related_symbols.py` reprocessing CLI defaults to
dry-run behind a default-disabled env gate.
"""

from __future__ import annotations

import random
from datetime import datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.schemas.news import NewsBulkIngestRequest
from app.services.llm_news_service import ingest_news_ingestor_bulk


def _unique_symbol() -> str:
    return f"9{random.randint(10000, 99999)}"


def _kr_article_payload(*, run_uuid: str, title: str, raw: dict | None = None) -> dict:
    suffix = uuid4().hex[:12]
    return {
        "ingestion_run": {
            "run_uuid": run_uuid,
            "market": "kr",
            "feed_set": "kr-core",
            "started_at": "2026-07-15T00:00:00+00:00",
            "finished_at": "2026-07-15T00:05:00+00:00",
            "source_counts": {"http_naver_stock_aggregate": 1},
        },
        "articles": [
            {
                "fingerprint": f"rob916-{suffix}",
                "market": "kr",
                "source": "http_naver_stock_aggregate",
                "title": title,
                "url": f"https://n.news.naver.com/mnews/article/rob916/{suffix}",
                "canonical_url": f"https://n.news.naver.com/mnews/article/rob916/{suffix}",
                "publisher": "연합뉴스",
                "published_at": "2026-07-15T10:00:00+09:00",
                "summary": None,
                "raw": raw or {},
            }
        ],
    }


@pytest_asyncio.fixture
async def synthetic_kr_universe_row(db_session):
    """Insert+commit a synthetic (symbol, name) row so a second session sees it."""
    symbol = _unique_symbol()
    name = f"테스트한화오션{symbol[-3:]}"
    db_session.add(
        KRSymbolUniverse(symbol=symbol, name=name, exchange="KOSPI", is_active=True)
    )
    await db_session.commit()
    try:
        yield symbol, name
    finally:
        await db_session.execute(
            delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == symbol)
        )
        await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_bulk_maps_missed_symbol_via_kr_universe_name(
    db_session, synthetic_kr_universe_row
):
    """Reproduces the 한화오션 evidence case: ingestor sends NO stock_candidates
    for a KR article whose title explicitly names the company — the
    kr_symbol_universe_name supplementary match must still create the
    news_article_related_symbols row."""
    symbol, name = synthetic_kr_universe_row
    run_uuid = f"test-rob916-{uuid4().hex}"
    payload = _kr_article_payload(
        run_uuid=run_uuid,
        title=f"{name}, 3943억 규모 VLCC 2척 수주",
        raw={},  # no stock_candidates / related_symbols / tv_related_symbols
    )

    request = NewsBulkIngestRequest.model_validate(payload)
    response = await ingest_news_ingestor_bulk(request)
    assert response.success
    assert response.inserted_count == 1

    target_url = payload["articles"][0]["canonical_url"]
    article_id = (
        await db_session.execute(
            select(NewsArticle.id).where(NewsArticle.url == target_url)
        )
    ).scalar_one()

    rows = (
        (
            await db_session.execute(
                select(NewsArticleRelatedSymbol).where(
                    NewsArticleRelatedSymbol.article_id == article_id
                )
            )
        )
        .scalars()
        .all()
    )

    assert any(
        row.symbol == symbol and row.source == "kr_symbol_universe_name" for row in rows
    ), f"expected a kr_symbol_universe_name row for {symbol}, got {rows}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_bulk_does_not_duplicate_when_ingestor_already_tagged_symbol(
    db_session, synthetic_kr_universe_row
):
    """When the ingestor's own candidate metadata already covers the symbol,
    the kr_symbol_universe_name matcher must not add a redundant duplicate
    row for the same (article, symbol) — additive, not double-booking."""
    symbol, name = synthetic_kr_universe_row
    run_uuid = f"test-rob916-{uuid4().hex}"
    payload = _kr_article_payload(
        run_uuid=run_uuid,
        title=f"{name}, 3943억 규모 VLCC 2척 수주",
        raw={
            "stock_candidates": [
                {"symbol": symbol, "market": "kr", "name": name, "score": 0.9}
            ]
        },
    )

    request = NewsBulkIngestRequest.model_validate(payload)
    response = await ingest_news_ingestor_bulk(request)
    assert response.success

    target_url = payload["articles"][0]["canonical_url"]
    article_id = (
        await db_session.execute(
            select(NewsArticle.id).where(NewsArticle.url == target_url)
        )
    ).scalar_one()

    rows = (
        (
            await db_session.execute(
                select(NewsArticleRelatedSymbol).where(
                    NewsArticleRelatedSymbol.article_id == article_id,
                    NewsArticleRelatedSymbol.symbol == symbol,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1
    assert rows[0].source == "candidate_metadata"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_bulk_us_article_does_not_query_kr_universe(db_session):
    """Non-KR articles must not trigger the KR-only universe matcher path
    (no crash, no spurious symbol rows)."""
    run_uuid = f"test-rob916-us-{uuid4().hex}"
    suffix = uuid4().hex[:12]
    payload = {
        "ingestion_run": {
            "run_uuid": run_uuid,
            "market": "us",
            "feed_set": "us-core",
            "started_at": "2026-07-15T00:00:00+00:00",
            "finished_at": "2026-07-15T00:05:00+00:00",
            "source_counts": {"rss_cnbc_us_markets": 1},
        },
        "articles": [
            {
                "fingerprint": f"rob916-us-{suffix}",
                "market": "us",
                "source": "rss_cnbc_us_markets",
                "title": "Fed holds rates steady",
                "url": f"https://example.com/us/{suffix}",
                "canonical_url": f"https://example.com/us/{suffix}",
                "publisher": "CNBC",
                "published_at": "2026-07-15T10:00:00+09:00",
                "summary": None,
                "raw": {},
            }
        ],
    }

    request = NewsBulkIngestRequest.model_validate(payload)
    response = await ingest_news_ingestor_bulk(request)
    assert response.success
    assert response.inserted_count == 1


class TestBackfillScriptCLI:
    def test_default_is_dry_run(self):
        from scripts.backfill_news_related_symbols import parse_args

        args = parse_args([])
        assert args.apply is False
        assert args.dry_run is True

    def test_apply_flag(self):
        from scripts.backfill_news_related_symbols import parse_args

        args = parse_args(["--apply"])
        assert args.apply is True
        assert args.dry_run is False

    def test_default_focus_symbols_include_tier_b_evidence_symbols(self):
        from scripts.backfill_news_related_symbols import parse_args

        args = parse_args([])
        symbols = args.focus_symbols.split(",")
        assert "042660" in symbols  # 한화오션
        assert "279570" in symbols  # 케이뱅크
        assert "476060" in symbols  # 온코닉테라퓨틱스


@pytest.mark.asyncio
async def test_backfill_main_refuses_when_env_gate_disabled(monkeypatch):
    from app.core.config import settings
    from scripts import backfill_news_related_symbols as script

    monkeypatch.setattr(
        settings, "NEWS_RELATED_SYMBOLS_BACKFILL_ENABLED", False, raising=False
    )
    monkeypatch.setattr("sys.argv", ["backfill_news_related_symbols.py"])

    exit_code = await script.main()

    assert exit_code == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write_and_reports_recall(
    db_session, synthetic_kr_universe_row
):
    """Dry-run must compute the recall report (would-be-mapped rows) without
    persisting anything — this is the mode ROB-916 requires against prod."""
    from scripts.backfill_news_related_symbols import run_backfill

    symbol, name = synthetic_kr_universe_row
    now = datetime(2026, 7, 15, 10, 0, 0)
    article = NewsArticle(
        url=f"https://n.news.naver.com/mnews/article/rob916-backfill/{uuid4().hex[:12]}",
        title=f"{name} 캐나다 수주실패 선반영",
        market="kr",
        feed_source="http_naver_stock_aggregate",
        article_published_at=now,
        scraped_at=now,
        created_at=now,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    try:
        result = await run_backfill(
            from_date="2026-07-14",
            to_date="2026-07-17",
            feed_source="http_naver_stock_aggregate",
            focus_symbols=[symbol],
            apply=False,
        )

        assert result.applied is False
        matching_rows = [r for r in result.recall if r.symbol == symbol]
        assert matching_rows
        assert matching_rows[0].newly_mapped >= 1

        # Dry-run: nothing written.
        existing = (
            (
                await db_session.execute(
                    select(NewsArticleRelatedSymbol).where(
                        NewsArticleRelatedSymbol.article_id == article.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert existing == []

        # Now apply=True and confirm it writes exactly once, idempotently.
        result_apply = await run_backfill(
            from_date="2026-07-14",
            to_date="2026-07-17",
            feed_source="http_naver_stock_aggregate",
            focus_symbols=[symbol],
            apply=True,
        )
        assert result_apply.applied is True
        assert result_apply.new_rows >= 1

        written = (
            (
                await db_session.execute(
                    select(NewsArticleRelatedSymbol).where(
                        NewsArticleRelatedSymbol.article_id == article.id,
                        NewsArticleRelatedSymbol.symbol == symbol,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(written) == 1
        assert written[0].source == "kr_symbol_universe_name"

        # Re-running apply must be idempotent (on_conflict_do_nothing).
        result_rerun = await run_backfill(
            from_date="2026-07-14",
            to_date="2026-07-17",
            feed_source="http_naver_stock_aggregate",
            focus_symbols=[symbol],
            apply=True,
        )
        rerun_matching = [r for r in result_rerun.recall if r.symbol == symbol]
        assert rerun_matching[0].newly_mapped == 0
        assert rerun_matching[0].already_mapped >= 1
    finally:
        await db_session.execute(
            delete(NewsArticleRelatedSymbol).where(
                NewsArticleRelatedSymbol.article_id == article.id
            )
        )
        await db_session.execute(
            delete(NewsArticle).where(NewsArticle.id == article.id)
        )
        await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_related_symbols_is_idempotent(db_session):
    from app.services import symbol_news_store

    now = datetime(2026, 7, 15, 10, 0, 0)
    article = NewsArticle(
        url=f"https://example.com/rob916-upsert/{uuid4().hex[:12]}",
        title="테스트 기사",
        market="kr",
        feed_source="http_naver_stock_aggregate",
        article_published_at=now,
        scraped_at=now,
        created_at=now,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    row = {
        "article_id": article.id,
        "market": "kr",
        "symbol": _unique_symbol(),
        "display_name": "테스트종목",
        "source": "kr_symbol_universe_name",
        "matched_term": "테스트종목",
        "score": None,
        "rank": None,
        "raw": {"matcher": "kr_symbol_universe_name"},
        "created_at": now,
    }

    try:
        first = await symbol_news_store.upsert_related_symbols(db_session, [row])
        second = await symbol_news_store.upsert_related_symbols(db_session, [row])

        assert first == 1
        assert second == 0

        rows = (
            (
                await db_session.execute(
                    select(NewsArticleRelatedSymbol).where(
                        NewsArticleRelatedSymbol.article_id == article.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
    finally:
        await db_session.execute(
            delete(NewsArticleRelatedSymbol).where(
                NewsArticleRelatedSymbol.article_id == article.id
            )
        )
        await db_session.execute(
            delete(NewsArticle).where(NewsArticle.id == article.id)
        )
        await db_session.commit()
