"""DB proof for scripts/prune_nonprice_history.py (hk 5751, CI only).

``--commit`` deletes exactly the targeted rows and their dependents, keeps the
kept-list rows and the latest Toss row per symbol, works in batches of one
row, and a second run is a no-op.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.db import AsyncSessionLocal
from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventValue,
)
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.news import NewsAnalysisResult, NewsArticle, NewsArticleRelatedSymbol
from app.models.symbol_news_relevance import SymbolNewsRelevance
from scripts import prune_nonprice_history as prune

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

RETIRED_FEED = "rss_cnbc_finance"
KEPT_FEED = "naver_item_news"
NOW = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)  # timestamptz columns (MarketEvent)
NOW_NAIVE = datetime(2026, 11, 20, 0, 0)  # timestamp-without-time-zone columns
MARK = uuid.uuid4().hex[:8]  # keeps this run's symbols and urls apart from others


async def _seed(session) -> dict[str, object]:
    def article(feed: str, tag: str) -> NewsArticle:
        return NewsArticle(
            url=f"https://example.invalid/prune/{MARK}/{tag}",
            title=f"prune {tag}",
            market="kr",
            feed_source=feed,
            scraped_at=NOW_NAIVE,
            created_at=NOW_NAIVE,
        )

    retired_a = article(RETIRED_FEED, "retired-a")
    retired_b = article(RETIRED_FEED, "retired-b")
    shared = article(RETIRED_FEED, "shared")  # first seen on a retired feed
    kept = article(KEPT_FEED, "kept")
    session.add_all([retired_a, retired_b, shared, kept])
    await session.flush()

    symbol = f"P{MARK[:5].upper()}"
    for art in (retired_a, shared, kept):
        session.add(
            SymbolNewsRelevance(
                article_id=art.id,
                market="kr",
                symbol=symbol,
                feed_source=KEPT_FEED if art is shared else art.feed_source,
                first_seen_at=NOW_NAIVE,
                status="pending",
                created_at=NOW_NAIVE,
                updated_at=NOW_NAIVE,
            )
        )
    session.add(
        NewsArticleRelatedSymbol(
            article_id=retired_a.id,
            market="kr",
            symbol=symbol,
            source="fixture",
            created_at=NOW_NAIVE,
        )
    )
    session.add(
        NewsAnalysisResult(
            article_id=retired_a.id,
            model_name="fixture",
            sentiment="neutral",
            summary="fixture",
            key_points=[],
            confidence=50,
            prompt="fixture",
            raw_response="fixture",
            created_at=NOW_NAIVE,
        )
    )

    tv_event = MarketEvent(
        category="economic",
        market="global",
        event_date=date(2026, 6, 1),
        source="tradingview",
        source_event_id=f"tv-{MARK}",
        created_at=NOW,
    )
    dart_event = MarketEvent(
        category="disclosure",
        market="kr",
        event_date=date(2026, 6, 1),
        source="dart",
        source_event_id=f"dart-{MARK}",
        created_at=NOW,
    )
    session.add_all([tv_event, dart_event])
    await session.flush()
    session.add(MarketEventValue(event_id=tv_event.id, metric_name="cpi"))
    session.add(
        MarketEventIngestionPartition(
            source="tradingview",
            category="economic",
            market="global",
            partition_date=date(2026, 6, 1),
        )
    )
    session.add(
        MarketEventIngestionPartition(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=date(2026, 6, 1),
        )
    )

    toss_symbol = f"T{MARK[:5].upper()}"
    single_symbol = f"S{MARK[:5].upper()}"
    for offset in range(3):
        session.add(
            MarketValuationSnapshot(
                market="kr",
                symbol=toss_symbol,
                snapshot_date=date(2026, 6, 1) + timedelta(days=offset),
                source="toss_openapi",
                market_cap=1000 + offset,
            )
        )
    session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol=single_symbol,
            snapshot_date=date(2026, 6, 1),
            source="toss_openapi",
            market_cap=5,
        )
    )
    session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol=toss_symbol,
            snapshot_date=date(2026, 6, 1),
            source="naver_finance",
            per=7,
            pbr=1,
        )
    )
    await session.commit()
    return {
        "retired_a": retired_a.id,
        "retired_b": retired_b.id,
        "shared": shared.id,
        "kept": kept.id,
        "tv_event": tv_event.id,
        "dart_event": dart_event.id,
        "toss_symbol": toss_symbol,
        "single_symbol": single_symbol,
    }


async def _cleanup(session) -> None:
    await session.execute(
        delete(NewsArticle).where(
            NewsArticle.url.like(f"https://example.invalid/prune/{MARK}/%")
        )
    )
    await session.execute(
        delete(MarketEvent).where(
            MarketEvent.source_event_id.in_([f"tv-{MARK}", f"dart-{MARK}"])
        )
    )
    await session.execute(
        delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.partition_date == date(2026, 6, 1),
            MarketEventIngestionPartition.source.in_(["tradingview", "dart"]),
        )
    )
    await session.execute(
        delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(
                [f"T{MARK[:5].upper()}", f"S{MARK[:5].upper()}"]
            )
        )
    )
    await session.commit()


async def _ids(session, model, column_ids):
    return set(
        (
            await session.execute(select(model.id).where(model.id.in_(column_ids)))
        ).scalars()
    )


@pytest_asyncio.fixture
async def seeded():
    async with AsyncSessionLocal() as session:
        ids = await _seed(session)
    try:
        yield ids
    finally:
        async with AsyncSessionLocal() as session:
            await _cleanup(session)


async def test_preview_counts_our_rows_and_writes_nothing(seeded) -> None:
    report = await prune.run_preview(AsyncSessionLocal)
    news = report["categories"]["news"]
    assert news["by_feed"][RETIRED_FEED]["protected_by_retained_feed_link"] >= 1
    assert news["by_feed"][RETIRED_FEED]["articles"] >= 3
    assert news["by_feed"][RETIRED_FEED]["symbol_news_relevance"] >= 1
    assert news["by_feed"][RETIRED_FEED]["news_article_related_symbols"] >= 1
    assert news["by_feed"][RETIRED_FEED]["news_analysis_results"] >= 1
    assert report["categories"]["toss_valuation"]["matched"] >= 2

    async with AsyncSessionLocal() as session:
        assert seeded["retired_a"] in await _ids(
            session, NewsArticle, [seeded["retired_a"]]
        )


async def test_commit_deletes_exactly_the_targets_and_dependents_in_one_row_batches(
    seeded,
) -> None:
    report = await prune.run_commit(AsyncSessionLocal, batch_size=1)
    assert report["mode"] == "commit"
    assert report["held_news_feeds_kept"] == ["http_naver_stock_aggregate"]

    async with AsyncSessionLocal() as session:
        # Retired article rows and every dependent of them are gone.
        assert (
            await _ids(session, NewsArticle, [seeded["retired_a"], seeded["retired_b"]])
            == set()
        )
        assert (
            await session.execute(
                select(SymbolNewsRelevance.id).where(
                    SymbolNewsRelevance.article_id == seeded["retired_a"]
                )
            )
        ).first() is None
        assert (
            await session.execute(
                select(NewsArticleRelatedSymbol.id).where(
                    NewsArticleRelatedSymbol.article_id == seeded["retired_a"]
                )
            )
        ).first() is None
        assert (
            await session.execute(
                select(NewsAnalysisResult.id).where(
                    NewsAnalysisResult.article_id == seeded["retired_a"]
                )
            )
        ).first() is None

        # An article a retained feed also links is protected and survives.
        assert await _ids(session, NewsArticle, [seeded["shared"]]) == {
            seeded["shared"]
        }

        # Kept feed and its relevance row survive.
        assert await _ids(session, NewsArticle, [seeded["kept"]]) == {seeded["kept"]}
        assert (
            await session.execute(
                select(SymbolNewsRelevance.id).where(
                    SymbolNewsRelevance.article_id == seeded["kept"]
                )
            )
        ).first() is not None

        # Retired market-event triple gone with its values and partition; DART kept.
        assert await _ids(session, MarketEvent, [seeded["tv_event"]]) == set()
        assert await _ids(session, MarketEvent, [seeded["dart_event"]]) == {
            seeded["dart_event"]
        }
        assert (
            await session.execute(
                select(MarketEventValue.id).where(
                    MarketEventValue.event_id == seeded["tv_event"]
                )
            )
        ).first() is None
        assert (
            await session.execute(
                select(MarketEventIngestionPartition.id).where(
                    MarketEventIngestionPartition.source == "tradingview"
                )
            )
        ).first() is None
        assert (
            await session.execute(
                select(MarketEventIngestionPartition.id).where(
                    MarketEventIngestionPartition.source == "dart"
                )
            )
        ).first() is not None

        # Toss: only the latest row per symbol remains; other source untouched.
        toss_rows = (
            await session.execute(
                select(
                    MarketValuationSnapshot.snapshot_date,
                    MarketValuationSnapshot.source,
                ).where(
                    MarketValuationSnapshot.symbol == seeded["toss_symbol"],
                    MarketValuationSnapshot.market == "kr",
                )
            )
        ).all()
        assert sorted((row.source, row.snapshot_date) for row in toss_rows) == [
            ("naver_finance", date(2026, 6, 1)),
            ("toss_openapi", date(2026, 6, 3)),
        ]
        single = (
            (
                await session.execute(
                    select(MarketValuationSnapshot.snapshot_date).where(
                        MarketValuationSnapshot.symbol == seeded["single_symbol"],
                        MarketValuationSnapshot.market == "kr",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert single == [date(2026, 6, 1)]


async def test_second_commit_is_a_no_op(seeded) -> None:
    await prune.run_commit(AsyncSessionLocal, batch_size=1)
    second = await prune.run_commit(AsyncSessionLocal, batch_size=1)
    assert second["categories"]["news"]["articles_deleted"] == 0
    assert second["categories"]["news"]["batches"] == 0
    assert second["categories"]["toss_valuation"]["toss_rows_deleted"] == 0
    for triple_report in second["categories"]["market_events"].values():
        assert triple_report["events_deleted"] == 0
        assert triple_report["partitions_deleted"] == 0
