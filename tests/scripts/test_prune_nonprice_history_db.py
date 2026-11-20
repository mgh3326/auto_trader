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
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

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
MARK = uuid.uuid4().hex[:8].upper()  # tags this run's urls, event ids and symbols
# A partition date no other fixture uses; partitions carry no marker of their own.
PARTITION_DATE = date(2100, 1, 1) + timedelta(days=int(MARK, 16) % 3000)
SCOPE = prune.PruneScope(marker=MARK, partition_dates=frozenset({PARTITION_DATE}))


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

    symbol = f"P{MARK}"
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
            partition_date=PARTITION_DATE,
        )
    )
    session.add(
        MarketEventIngestionPartition(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=PARTITION_DATE,
        )
    )

    toss_symbol = f"T{MARK}"
    single_symbol = f"S{MARK}"
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
            MarketEventIngestionPartition.partition_date == PARTITION_DATE,
            MarketEventIngestionPartition.source.in_(["tradingview", "dart"]),
        )
    )
    await session.execute(
        delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_([f"T{MARK}", f"S{MARK}"])
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
    report = await prune.run_commit(AsyncSessionLocal, batch_size=1, scope=SCOPE)
    assert report["mode"] == "commit"
    assert report["held_news_feeds_kept"] == ["http_naver_stock_aggregate"]

    # Exact counts for this run's fixture rows only (the scope excludes other rows).
    news = report["categories"]["news"]
    assert news["articles_deleted"] == 2  # retired_a and retired_b; shared is protected
    assert news["dependents_deleted"] == {
        "symbol_news_relevance": 1,
        "news_article_related_symbols": 1,
        "news_analysis_results": 1,
    }
    tv = report["categories"]["market_events"]["tradingview/economic/global"]
    assert tv["events_deleted"] == 1
    assert tv["dependents_deleted"] == {"market_event_values": 1}
    assert tv["partitions_deleted"] == 1
    assert (
        report["categories"]["market_events"]["wisefn/earnings/kr"]["events_deleted"]
        == 0
    )
    assert report["categories"]["toss_valuation"]["toss_rows_deleted"] == 2

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
                    MarketEventIngestionPartition.source == "tradingview",
                    MarketEventIngestionPartition.partition_date == PARTITION_DATE,
                )
            )
        ).first() is None
        assert (
            await session.execute(
                select(MarketEventIngestionPartition.id).where(
                    MarketEventIngestionPartition.source == "dart",
                    MarketEventIngestionPartition.partition_date == PARTITION_DATE,
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
    await prune.run_commit(AsyncSessionLocal, batch_size=1, scope=SCOPE)
    second = await prune.run_commit(AsyncSessionLocal, batch_size=1, scope=SCOPE)
    assert second["categories"]["news"]["articles_deleted"] == 0
    assert second["categories"]["news"]["batches"] == 0
    assert second["categories"]["toss_valuation"]["toss_rows_deleted"] == 0
    for triple_report in second["categories"]["market_events"].values():
        assert triple_report["events_deleted"] == 0
        assert triple_report["partitions_deleted"] == 0


async def test_failed_batch_rolls_back_while_the_earlier_batch_stays_committed(
    seeded,
) -> None:
    """Batch 1 (retired_a) commits. Batch 2 (retired_b) gets a dependent delete
    run first, then a test-local trigger refuses its parent delete. Batch 2 must
    roll back entirely: retired_b and its dependent remain, and retired_a stays gone."""

    trigger = f"pytest_prune_block_{MARK.lower()}"
    later_id = int(seeded["retired_b"])
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                f"CREATE OR REPLACE FUNCTION {trigger}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                f"IF OLD.id = {later_id} THEN RAISE EXCEPTION 'prune rollback probe'; END IF; "
                "RETURN OLD; END $$"
            )
        )
        await session.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE DELETE ON news_articles "
                f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
            )
        )
        session.add(
            SymbolNewsRelevance(
                article_id=later_id,
                market="kr",
                symbol=f"P{MARK}",
                feed_source=RETIRED_FEED,
                first_seen_at=NOW_NAIVE,
                status="pending",
                created_at=NOW_NAIVE,
                updated_at=NOW_NAIVE,
            )
        )
        await session.commit()
    try:
        with pytest.raises(DBAPIError):
            await prune.run_commit(
                AsyncSessionLocal, batch_size=1, only=["news"], scope=SCOPE
            )

        async with AsyncSessionLocal() as session:  # fresh session reads what persisted
            assert await _ids(session, NewsArticle, [seeded["retired_a"]]) == set()
            assert (
                await session.execute(
                    select(SymbolNewsRelevance.id).where(
                        SymbolNewsRelevance.article_id == seeded["retired_a"]
                    )
                )
            ).first() is None
            assert await _ids(session, NewsArticle, [later_id]) == {later_id}
            assert (
                await session.execute(
                    select(SymbolNewsRelevance.id).where(
                        SymbolNewsRelevance.article_id == later_id
                    )
                )
            ).first() is not None
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger} ON news_articles")
            )
            await session.execute(text(f"DROP FUNCTION IF EXISTS {trigger}()"))
            await session.commit()
