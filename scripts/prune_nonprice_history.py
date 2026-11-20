"""Operator CLI: prune retired non-price history rows (hk 5751).

Preview by default. ``--commit`` deletes in bounded batches; every batch is its
own transaction, the run is resumable (it walks ids forward from the last
batch), and a second run is a no-op once nothing matches.

Categories
- ``news``: 20 retired ``news_articles.feed_source`` keys. Their dependents
  (``symbol_news_relevance``, ``news_article_related_symbols``,
  ``news_analysis_results``) are deleted first in the same batch transaction.
  The schema also declares these as ON DELETE CASCADE; the explicit delete keeps
  the counts honest and the order visible.
- ``market_events``: three retired ``(source, category, market)`` triples, with
  their ``market_event_values`` rows and their ``market_event_ingestion_partitions``
  rows.
- ``toss_valuation``: ``market_valuation_snapshots`` rows with
  ``source='toss_openapi'`` that a strictly newer Toss row of the same
  ``(market, symbol)`` supersedes. Latest row per symbol is kept.

Kept, never selected: news ``naver_item_news``, ``finnhub_company_news``,
``finnhub_general_news``, ``research_on_demand_naver``,
``research_on_demand_finnhub``; market events ``dart`` and ``finnhub``; valuation
``naver_finance`` and ``tvscreener`` rows.

Held feeds: ``http_naver_stock_aggregate`` is a retired feed that the spike
attribution spec still reads as its only exact-timestamp KR evidence source.
It is excluded from ``--commit`` unless ``--allow-held-feed`` names it
explicitly (hk 5751 ESC, default = held).

The CLI prints counts only. It never prints URLs, titles, symbols or payloads.
It is not scheduled. The design session runs it on the pi after deploy, then
VACUUMs.

Usage::

    uv run python -m scripts.prune_nonprice_history --database-url-env NAME
    uv run python -m scripts.prune_nonprice_history --database-url-env NAME --commit

Exit codes: 0 preview or commit finished, 1 input or database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.market_events import (
    MarketEvent,
    MarketEventIngestionPartition,
    MarketEventValue,
)
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.news import NewsAnalysisResult, NewsArticle, NewsArticleRelatedSymbol
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.market_valuation_snapshots.repository import superseded_by_newer_row

EXIT_OK = 0
EXIT_ERROR = 1

DEFAULT_BATCH_SIZE = 5000
MAX_BATCH_SIZE = 50_000

TOSS_VALUATION_SOURCE = "toss_openapi"

NEWS_RETIRED_FEEDS: tuple[str, ...] = (
    "rss_yahoo_finance_topstories",
    "http_naver_stock_aggregate",
    "browser_naver_mainnews",
    "rss_cnbc_us_markets",
    "browser_naver_research_company",
    "rss_marketwatch_topstories",
    "rss_cointelegraph",
    "rss_coindesk",
    "browser_naver_research",
    "browser_naver_research_industry",
    "rss_decrypt",
    "browser_naver_research_invest",
    "rss_bitcoin_magazine",
    "browser_naver_research_economy",
    "browser_investing_news",
    "browser_cnbc_news",
    "rss_cnbc_earnings",
    "rss_fed_press",
    "rss_cnbc_finance",
    "http_tvscreener_news_kr",
)
NEWS_KEPT_FEEDS: tuple[str, ...] = (
    "naver_item_news",
    "finnhub_company_news",
    "finnhub_general_news",
    "research_on_demand_naver",
    "research_on_demand_finnhub",
)
HELD_NEWS_FEEDS: frozenset[str] = frozenset({"http_naver_stock_aggregate"})

MARKET_EVENT_RETIRED_TRIPLES: tuple[tuple[str, str, str], ...] = (
    ("tradingview", "economic", "global"),
    ("forexfactory", "economic", "global"),
    ("wisefn", "earnings", "kr"),
)

CATEGORY_NEWS = "news"
CATEGORY_MARKET_EVENTS = "market_events"
CATEGORY_TOSS_VALUATION = "toss_valuation"
ALL_CATEGORIES: tuple[str, ...] = (
    CATEGORY_NEWS,
    CATEGORY_MARKET_EVENTS,
    CATEGORY_TOSS_VALUATION,
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PruneInputError(ValueError):
    """Invalid arguments or an invalid database URL."""


# ---------------------------------------------------------------------------
# Pure selection (unit-tested, no database)
# ---------------------------------------------------------------------------


def news_feeds_to_delete(allow_held: Iterable[str] = ()) -> tuple[str, ...]:
    """Retired news feeds in declaration order, minus held feeds not allowed.

    Unknown names in ``allow_held`` are an input error, so a typo cannot
    silently leave a feed held.
    """

    allowed = frozenset(allow_held)
    unknown = allowed - HELD_NEWS_FEEDS
    if unknown:
        raise PruneInputError(
            "--allow-held-feed accepts only held feeds: "
            + ", ".join(sorted(HELD_NEWS_FEEDS))
        )
    return tuple(
        feed
        for feed in NEWS_RETIRED_FEEDS
        if feed not in HELD_NEWS_FEEDS or feed in allowed
    )


def held_news_feeds_not_allowed(allow_held: Iterable[str] = ()) -> tuple[str, ...]:
    allowed = frozenset(allow_held)
    return tuple(sorted(feed for feed in HELD_NEWS_FEEDS if feed not in allowed))


def assert_selection_disjoint_from_keep() -> None:
    """Fail fast if a kept feed or triple is ever added to a delete list."""

    overlap = set(NEWS_RETIRED_FEEDS) & set(NEWS_KEPT_FEEDS)
    if overlap:
        raise AssertionError(
            f"retired news feeds overlap kept feeds: {sorted(overlap)}"
        )
    if len(set(NEWS_RETIRED_FEEDS)) != len(NEWS_RETIRED_FEEDS):
        raise AssertionError("duplicate retired news feed")
    if len(set(MARKET_EVENT_RETIRED_TRIPLES)) != len(MARKET_EVENT_RETIRED_TRIPLES):
        raise AssertionError("duplicate retired market event triple")


assert_selection_disjoint_from_keep()


def batch_size_or_raise(value: int) -> int:
    if not 1 <= value <= MAX_BATCH_SIZE:
        raise PruneInputError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    return value


# ---------------------------------------------------------------------------
# Batched execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """One committed batch. ``selected`` 0 means the category is exhausted."""

    selected: int
    deleted: int
    last_id: int | None
    dependents: dict[str, int] = field(default_factory=dict)


StepFn = Callable[[int, int], Awaitable[StepResult]]


async def run_batches(step: StepFn, batch_size: int) -> tuple[int, dict[str, int], int]:
    """Walk ids forward one committed batch at a time until nothing is selected.

    Returns ``(total_deleted, dependent_totals, batches)``. A batch whose
    deleted-row count differs from its selected-id count is a race or a bug; the
    step's transaction has already rolled back, so raise instead of continuing.
    """

    after = 0
    total = 0
    batches = 0
    dependent_totals: dict[str, int] = {}
    while True:
        result = await step(after, batch_size)
        if result.selected == 0:
            break
        if result.selected > batch_size:
            raise RuntimeError("batch exceeded its bound")
        if result.deleted != result.selected:
            raise RuntimeError(
                f"batch deleted {result.deleted} of {result.selected} selected rows"
            )
        if result.last_id is None or result.last_id <= after:
            raise RuntimeError("batch cursor did not advance")
        total += result.deleted
        batches += 1
        for name, count in result.dependents.items():
            dependent_totals[name] = dependent_totals.get(name, 0) + count
        after = result.last_id
    return total, dependent_totals, batches


# ---------------------------------------------------------------------------
# Database steps (each batch = one transaction)
# ---------------------------------------------------------------------------


def _news_step(
    session_factory: async_sessionmaker[AsyncSession], feeds: Sequence[str]
) -> StepFn:
    async def step(after: int, size: int) -> StepResult:
        async with session_factory() as session, session.begin():
            ids = list(
                (
                    await session.execute(
                        select(NewsArticle.id)
                        .where(
                            NewsArticle.feed_source.in_(feeds), NewsArticle.id > after
                        )
                        .order_by(NewsArticle.id)
                        .limit(size)
                    )
                ).scalars()
            )
            if not ids:
                return StepResult(selected=0, deleted=0, last_id=None)
            relevance = await session.execute(
                delete(SymbolNewsRelevance).where(
                    SymbolNewsRelevance.article_id.in_(ids)
                )
            )
            related = await session.execute(
                delete(NewsArticleRelatedSymbol).where(
                    NewsArticleRelatedSymbol.article_id.in_(ids)
                )
            )
            analysis = await session.execute(
                delete(NewsAnalysisResult).where(NewsAnalysisResult.article_id.in_(ids))
            )
            articles = await session.execute(
                delete(NewsArticle).where(NewsArticle.id.in_(ids))
            )
            return StepResult(
                selected=len(ids),
                deleted=articles.rowcount or 0,
                last_id=ids[-1],
                dependents={
                    "symbol_news_relevance": relevance.rowcount or 0,
                    "news_article_related_symbols": related.rowcount or 0,
                    "news_analysis_results": analysis.rowcount or 0,
                },
            )

    return step


def _market_event_step(
    session_factory: async_sessionmaker[AsyncSession],
    triple: tuple[str, str, str],
) -> StepFn:
    source, category, market = triple

    async def step(after: int, size: int) -> StepResult:
        async with session_factory() as session, session.begin():
            ids = list(
                (
                    await session.execute(
                        select(MarketEvent.id)
                        .where(
                            MarketEvent.source == source,
                            MarketEvent.category == category,
                            MarketEvent.market == market,
                            MarketEvent.id > after,
                        )
                        .order_by(MarketEvent.id)
                        .limit(size)
                    )
                ).scalars()
            )
            if not ids:
                return StepResult(selected=0, deleted=0, last_id=None)
            values = await session.execute(
                delete(MarketEventValue).where(MarketEventValue.event_id.in_(ids))
            )
            events = await session.execute(
                delete(MarketEvent).where(MarketEvent.id.in_(ids))
            )
            return StepResult(
                selected=len(ids),
                deleted=events.rowcount or 0,
                last_id=ids[-1],
                dependents={"market_event_values": values.rowcount or 0},
            )

    return step


def _partition_step(
    session_factory: async_sessionmaker[AsyncSession],
    triple: tuple[str, str, str],
) -> StepFn:
    source, category, market = triple

    async def step(after: int, size: int) -> StepResult:
        async with session_factory() as session, session.begin():
            ids = list(
                (
                    await session.execute(
                        select(MarketEventIngestionPartition.id)
                        .where(
                            MarketEventIngestionPartition.source == source,
                            MarketEventIngestionPartition.category == category,
                            MarketEventIngestionPartition.market == market,
                            MarketEventIngestionPartition.id > after,
                        )
                        .order_by(MarketEventIngestionPartition.id)
                        .limit(size)
                    )
                ).scalars()
            )
            if not ids:
                return StepResult(selected=0, deleted=0, last_id=None)
            parts = await session.execute(
                delete(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.id.in_(ids)
                )
            )
            return StepResult(
                selected=len(ids), deleted=parts.rowcount or 0, last_id=ids[-1]
            )

    return step


def _toss_valuation_step(session_factory: async_sessionmaker[AsyncSession]) -> StepFn:
    async def step(after: int, size: int) -> StepResult:
        async with session_factory() as session, session.begin():
            ids = list(
                (
                    await session.execute(
                        select(MarketValuationSnapshot.id)
                        .where(
                            MarketValuationSnapshot.source == TOSS_VALUATION_SOURCE,
                            MarketValuationSnapshot.id > after,
                            superseded_by_newer_row(TOSS_VALUATION_SOURCE),
                        )
                        .order_by(MarketValuationSnapshot.id)
                        .limit(size)
                    )
                ).scalars()
            )
            if not ids:
                return StepResult(selected=0, deleted=0, last_id=None)
            rows = await session.execute(
                delete(MarketValuationSnapshot).where(
                    MarketValuationSnapshot.id.in_(ids)
                )
            )
            return StepResult(
                selected=len(ids), deleted=rows.rowcount or 0, last_id=ids[-1]
            )

    return step


# ---------------------------------------------------------------------------
# Preview (SELECT only)
# ---------------------------------------------------------------------------


async def _scalar(session: AsyncSession, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _news_preview(
    session: AsyncSession, feeds: Sequence[str], all_retired: Sequence[str]
) -> dict[str, Any]:
    by_feed: dict[str, dict[str, int]] = {}
    for feed in all_retired:
        by_feed[feed] = {
            "articles": 0,
            "symbol_news_relevance": 0,
            "news_article_related_symbols": 0,
            "news_analysis_results": 0,
        }
    article_rows = await session.execute(
        select(NewsArticle.feed_source, func.count(NewsArticle.id))
        .where(NewsArticle.feed_source.in_(all_retired))
        .group_by(NewsArticle.feed_source)
    )
    for feed, count in article_rows.all():
        by_feed[feed]["articles"] = int(count)
    for label, model in (
        ("symbol_news_relevance", SymbolNewsRelevance),
        ("news_article_related_symbols", NewsArticleRelatedSymbol),
        ("news_analysis_results", NewsAnalysisResult),
    ):
        rows = await session.execute(
            select(NewsArticle.feed_source, func.count(model.id))
            .join(model, model.article_id == NewsArticle.id)
            .where(NewsArticle.feed_source.in_(all_retired))
            .group_by(NewsArticle.feed_source)
        )
        for feed, count in rows.all():
            by_feed[feed][label] = int(count)
    selected_articles = sum(by_feed[feed]["articles"] for feed in feeds)
    return {
        "matched": selected_articles,
        "by_feed": by_feed,
        "selected_feeds": list(feeds),
        "held_not_selected": sorted(set(all_retired) - set(feeds)),
    }


async def _market_events_preview(session: AsyncSession) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source, category, market in MARKET_EVENT_RETIRED_TRIPLES:
        key = f"{source}/{category}/{market}"
        events = await _scalar(
            session,
            select(func.count(MarketEvent.id)).where(
                MarketEvent.source == source,
                MarketEvent.category == category,
                MarketEvent.market == market,
            ),
        )
        values = await _scalar(
            session,
            select(func.count(MarketEventValue.id))
            .join(MarketEvent, MarketEvent.id == MarketEventValue.event_id)
            .where(
                MarketEvent.source == source,
                MarketEvent.category == category,
                MarketEvent.market == market,
            ),
        )
        partitions = await _scalar(
            session,
            select(func.count(MarketEventIngestionPartition.id)).where(
                MarketEventIngestionPartition.source == source,
                MarketEventIngestionPartition.category == category,
                MarketEventIngestionPartition.market == market,
            ),
        )
        out[key] = {
            "matched": events,
            "market_event_values": values,
            "market_event_ingestion_partitions": partitions,
        }
    return out


async def _toss_valuation_preview(session: AsyncSession) -> dict[str, Any]:
    total = await _scalar(
        session,
        select(func.count(MarketValuationSnapshot.id)).where(
            MarketValuationSnapshot.source == TOSS_VALUATION_SOURCE
        ),
    )
    superseded = await _scalar(
        session,
        select(func.count(MarketValuationSnapshot.id)).where(
            MarketValuationSnapshot.source == TOSS_VALUATION_SOURCE,
            superseded_by_newer_row(TOSS_VALUATION_SOURCE),
        ),
    )
    return {"matched": superseded, "toss_rows_total": total}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _selected_categories(only: Sequence[str] | None) -> tuple[str, ...]:
    if not only:
        return ALL_CATEGORIES
    unknown = set(only) - set(ALL_CATEGORIES)
    if unknown:
        raise PruneInputError(f"unknown --only category: {sorted(unknown)}")
    return tuple(c for c in ALL_CATEGORIES if c in set(only))


async def run_preview(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    only: Sequence[str] | None = None,
    allow_held: Iterable[str] = (),
) -> dict[str, Any]:
    categories = _selected_categories(only)
    feeds = news_feeds_to_delete(allow_held)
    report: dict[str, Any] = {"mode": "preview", "categories": {}}
    async with session_factory() as session:
        if CATEGORY_NEWS in categories:
            report["categories"][CATEGORY_NEWS] = await _news_preview(
                session, feeds, NEWS_RETIRED_FEEDS
            )
            report["categories"][CATEGORY_NEWS]["held_feeds_kept"] = list(
                held_news_feeds_not_allowed(allow_held)
            )
        if CATEGORY_MARKET_EVENTS in categories:
            report["categories"][CATEGORY_MARKET_EVENTS] = await _market_events_preview(
                session
            )
        if CATEGORY_TOSS_VALUATION in categories:
            report["categories"][
                CATEGORY_TOSS_VALUATION
            ] = await _toss_valuation_preview(session)
    return report


async def run_commit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
    only: Sequence[str] | None = None,
    allow_held: Iterable[str] = (),
) -> dict[str, Any]:
    categories = _selected_categories(only)
    allowed = tuple(allow_held)
    held_kept = held_news_feeds_not_allowed(allowed)
    feeds = news_feeds_to_delete(allowed)
    report: dict[str, Any] = {
        "mode": "commit",
        "batch_size": batch_size,
        "held_news_feeds_kept": list(held_kept) if CATEGORY_NEWS in categories else [],
        "categories": {},
    }
    if CATEGORY_NEWS in categories:
        deleted, dependents, batches = await run_batches(
            _news_step(session_factory, feeds), batch_size
        )
        report["categories"][CATEGORY_NEWS] = {
            "articles_deleted": deleted,
            "dependents_deleted": dependents,
            "batches": batches,
            "selected_feeds": list(feeds),
        }
    if CATEGORY_MARKET_EVENTS in categories:
        events_report: dict[str, Any] = {}
        for triple in MARKET_EVENT_RETIRED_TRIPLES:
            key = "/".join(triple)
            events_deleted, dependents, batches = await run_batches(
                _market_event_step(session_factory, triple), batch_size
            )
            parts_deleted, _, parts_batches = await run_batches(
                _partition_step(session_factory, triple), batch_size
            )
            events_report[key] = {
                "events_deleted": events_deleted,
                "dependents_deleted": dependents,
                "batches": batches,
                "partitions_deleted": parts_deleted,
                "partition_batches": parts_batches,
            }
        report["categories"][CATEGORY_MARKET_EVENTS] = events_report
    if CATEGORY_TOSS_VALUATION in categories:
        deleted, _, batches = await run_batches(
            _toss_valuation_step(session_factory), batch_size
        )
        report["categories"][CATEGORY_TOSS_VALUATION] = {
            "toss_rows_deleted": deleted,
            "batches": batches,
        }
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _database_url(env_name: str) -> str:
    if not _ENV_NAME.fullmatch(env_name):
        raise PruneInputError(
            "--database-url-env must be an environment variable name "
            "(letters, digits, underscore; not starting with a digit)"
        )
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise PruneInputError(f"environment variable {env_name} is not set or empty")
    try:
        url = make_url(raw.strip())
    except Exception:
        raise PruneInputError(
            f"environment variable {env_name} does not hold a complete PostgreSQL URL"
        ) from None
    if url.get_backend_name() != "postgresql" or not url.database:
        raise PruneInputError(
            f"environment variable {env_name} does not hold a complete PostgreSQL URL"
        )
    return url.render_as_string(hide_password=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prune retired non-price history (news feeds, retired market-event "
            "triples, superseded Toss market-cap rows). Preview unless --commit."
        )
    )
    parser.add_argument(
        "--database-url-env",
        required=True,
        metavar="NAME",
        help="Name of the one environment variable that holds the PostgreSQL URL",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Delete in bounded batches; without it only counts are printed",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per transaction (1..{MAX_BATCH_SIZE}, default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=ALL_CATEGORIES,
        help="Restrict to one category (repeatable)",
    )
    parser.add_argument(
        "--allow-held-feed",
        action="append",
        default=[],
        metavar="FEED",
        help=(
            "Explicitly allow a held news feed to be deleted on --commit "
            "(held: " + ", ".join(sorted(HELD_NEWS_FEEDS)) + ")"
        ),
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    try:
        size = batch_size_or_raise(args.batch_size)
        url = _database_url(args.database_url_env)
        news_feeds_to_delete(args.allow_held_feed)  # reject unknown allow names early
    except PruneInputError as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}), file=sys.stderr)
        return EXIT_ERROR

    engine = create_async_engine(url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.commit:
            report = await run_commit(
                session_factory,
                batch_size=size,
                only=args.only,
                allow_held=args.allow_held_feed,
            )
        else:
            report = await run_preview(
                session_factory, only=args.only, allow_held=args.allow_held_feed
            )
    except PruneInputError as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # database or invariant failure; never echo the URL
        print(
            json.dumps({"status": "error", "error_class": type(exc).__name__}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    finally:
        await engine.dispose()

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
