"""Read-only source adapter for KR close-summary facts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.market_events import MarketEvent, MarketEventIngestionPartition
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.kr_market_close_summary.types import Evidence
from app.services.market_events.session_calendar import (
    next_trading_session,
    previous_trading_session,
    trading_session_status,
)

KST = ZoneInfo("Asia/Seoul")
MIN_MARKET_COVERAGE = Decimal("0.95")
MIN_FLOW_COVERAGE = Decimal("0.95")
DAILY_BARS_SETTLED_AT = time(7, 10)
DART_POST_CLOSE_COMPLETE_AT = time(18, 30)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def confirmed_flow_evidence(
    rows: Sequence[Mapping[str, Any]], universe_count: int | None = None
) -> dict[str, Any]:
    """Summarize confirmed Naver share quantities without adding across symbols."""
    confirmed = [row for row in rows if row.get("source") == "naver_finance"]
    if not confirmed:
        return Evidence(
            None,
            None,
            None,
            "confirmed_daily_flow_missing",
            _coverage(0, universe_count, MIN_FLOW_COVERAGE),
        ).to_dict()
    fields = ("foreign_net", "institution_net", "individual_net")
    complete = all(row.get(field) is not None for row in confirmed for field in fields)
    coverage = _coverage(len(confirmed), universe_count, MIN_FLOW_COVERAGE)
    value = (
        {
            **{
                key: count
                for field in fields
                for flow in (field.removesuffix("_net"),)
                for key, count in (
                    (
                        f"{flow}_net_buy_symbols",
                        sum(int(row[field]) > 0 for row in confirmed),
                    ),
                    (
                        f"{flow}_net_sell_symbols",
                        sum(int(row[field]) < 0 for row in confirmed),
                    ),
                )
            },
            "quantity_unit": "shares per symbol; counts are not market-wide flow",
            "symbol_snapshot_count": len(confirmed),
        }
        if complete
        else None
    )
    as_of = max(
        (row.get("collected_at") for row in confirmed if row.get("collected_at")),
        default=None,
    )
    return Evidence(
        value,
        "investor_flow_snapshots:naver_finance_confirmed_daily",
        _iso(as_of),
        "confirmed_flow_fields_missing"
        if not complete
        else "confirmed_flow_coverage_incomplete"
        if not coverage["complete"]
        else None,
        coverage,
    ).to_dict()


async def load_facts(session: AsyncSession, session_date: date) -> dict[str, Any]:
    now = datetime.now(KST)
    previous_session = previous_trading_session("kr", session_date)
    universe_count = await _load_active_symbol_count(session)
    flows = await _load_confirmed_flows(session, session_date, universe_count)
    bars = (
        await _load_session_bars(session, session_date, previous_session)
        if previous_session is not None
        else []
    )
    coverage = _coverage(
        sum(row["change_pct"] is not None for row in bars), universe_count
    )
    symbols = _top_mover_symbols(bars)
    disclosures, disclosures_complete = await _load_disclosures(
        session, session_date, symbols
    )
    next_session = next_trading_session("kr", session_date)
    bar_asof = (
        datetime.combine(next_session, DAILY_BARS_SETTLED_AT, KST)
        if next_session is not None
        else None
    )
    trading_status = trading_session_status("kr", session_date)
    return {
        "indices": await _load_indices(session, session_date),
        "investor_flows": flows,
        "market_breadth": _breadth(bars, session_date, coverage),
        "sectors": _sectors(bars, session_date, coverage),
        "top_movers": _top_movers(bars, session_date, coverage),
        "disclosures": disclosures,
        "news": await _load_news(session, session_date, symbols),
        "macro": Evidence(
            None,
            None,
            None,
            "no_confirmed_bok_rate_observation_stored",
        ).to_dict(),
        "_commit_readiness": {
            "session_confirmed": trading_status == "open",
            "session_is_past": session_date < now.date(),
            "data_settled": bar_asof is not None and now >= bar_asof,
            "market_coverage_complete": coverage["complete"],
            "flow_coverage_complete": flows["coverage"]["complete"],
            "disclosures_complete": disclosures_complete,
        },
    }


def _coverage(
    covered: int,
    universe: int | None,
    minimum_ratio: Decimal = MIN_MARKET_COVERAGE,
) -> dict[str, Any]:
    ratio = Decimal(covered) / Decimal(universe) if universe else Decimal(0)
    return {
        "covered_symbols": covered,
        "universe_symbols": universe,
        "ratio": str(ratio),
        "minimum_ratio": str(minimum_ratio),
        "complete": universe is not None and ratio >= minimum_ratio,
    }


async def _load_active_symbol_count(session: AsyncSession) -> int | None:
    result = await session.execute(
        text(
            "SELECT count(*) FROM public.kr_symbol_universe "
            "WHERE is_active IS TRUE AND is_common_share IS TRUE"
        )
    )
    return result.scalar_one_or_none()


async def _load_indices(session: AsyncSession, day: date) -> dict[str, Any]:
    # Only persisted KRX-session bars are eligible. Current quote providers are
    # intentionally not called by this manual, DB-backed CLI.
    rows = (
        (
            await session.execute(
                text(
                    """
                WITH history AS (
                    SELECT symbol, close, source, time,
                           lag(close) OVER (PARTITION BY symbol ORDER BY time)
                               AS prior_close
                    FROM public.kr_candles_1d
                    WHERE symbol IN ('KOSPI', 'KOSDAQ') AND venue = 'KRX'
                      AND time < :end_time
                )
                SELECT symbol, close, source, time, prior_close
                FROM history
                WHERE time::date = :session_date
                ORDER BY symbol, time DESC
                """
                ),
                {
                    "session_date": day,
                    "end_time": datetime.combine(
                        day + timedelta(days=1), time.min, tzinfo=KST
                    ),
                },
            )
        )
        .mappings()
        .all()
    )
    by_symbol: dict[str, Any] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], row)
    indices: dict[str, Any] = {}
    for symbol in ("KOSPI", "KOSDAQ"):
        row = by_symbol.get(symbol)
        value = None
        source = None
        as_of = None
        reason = "official_close_not_stored_for_session"
        if row is not None:
            close = _decimal(row["close"])
            prior_close = _decimal(row["prior_close"])
            change_pct = None
            if close is not None and prior_close not in (None, Decimal("0")):
                change_pct = (close - prior_close) / prior_close * Decimal("100")
            value = {
                "close": str(close) if close is not None else None,
                "change_pct": str(change_pct) if change_pct is not None else None,
                "basis": "KRX regular-session daily candle",
            }
            source = f"kr_candles_1d:{row['source']}"
            as_of = _iso(row["time"])
            reason = (
                "close_missing"
                if close is None
                else "prior_close_missing"
                if change_pct is None
                else None
            )
        indices[symbol] = Evidence(value, source, as_of, reason).to_dict()
    return indices


async def _load_confirmed_flows(
    session: AsyncSession, day: date, universe_count: int | None
) -> dict[str, Any]:
    # investor_flow_snapshots from the Naver daily trend collector are the
    # confirmed daily series. KIS intraday estimates and all other sources are
    # excluded by this predicate.
    rows = (
        await session.scalars(
            select(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.market == "kr",
                InvestorFlowSnapshot.snapshot_date == day,
                InvestorFlowSnapshot.source == "naver_finance",
            )
        )
    ).all()
    return confirmed_flow_evidence(
        [
            {
                "source": row.source,
                "foreign_net": row.foreign_net,
                "institution_net": row.institution_net,
                "individual_net": row.individual_net,
                "collected_at": row.collected_at,
            }
            for row in rows
        ],
        universe_count,
    )


async def _load_session_bars(
    session: AsyncSession, day: date, prior_session: date
) -> list[dict[str, Any]]:
    start = day - timedelta(days=10)
    rows = (
        (
            await session.execute(
                text(
                    """
                WITH bars AS (
                    SELECT c.time::date AS bar_date, c.symbol, c.close,
                           c.source, c.venue,
                           row_number() OVER (
                               PARTITION BY c.symbol, c.time::date
                               ORDER BY (c.venue = 'KRX') DESC, c.venue
                           ) AS venue_rank
                    FROM public.kr_candles_1d c
                    WHERE c.time >= :start_date AND c.time < :end_date
                      AND c.venue = 'KRX'
                ), prior AS (
                    SELECT DISTINCT ON (symbol) symbol, close, bar_date
                    FROM bars
                    WHERE bar_date = :prior_session_date AND venue_rank = 1
                    ORDER BY symbol
                ), current_day AS (
                    SELECT * FROM bars
                    WHERE bar_date = :session_date AND venue_rank = 1
                )
                SELECT u.symbol, u.name, u.exchange, u.sector_id,
                       s.name_kr AS sector_name, c.close AS current_close,
                       p.close AS prior_close, c.source, c.bar_date
                FROM current_day c
                JOIN public.kr_symbol_universe u ON u.symbol = c.symbol
                LEFT JOIN public.symbol_sectors s ON s.id = u.sector_id
                LEFT JOIN prior p ON p.symbol = c.symbol
                WHERE u.is_active IS TRUE AND u.is_common_share IS TRUE
                ORDER BY u.symbol
                """
                ),
                {
                    "start_date": start,
                    "end_date": day + timedelta(days=1),
                    "session_date": day,
                    "prior_session_date": prior_session,
                },
            )
        )
        .mappings()
        .all()
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        close = _decimal(row["current_close"])
        prior = _decimal(row["prior_close"])
        change_pct = None
        if close is not None and prior not in (None, Decimal("0")):
            change_pct = (close - prior) / prior * Decimal("100")
        result.append(
            {
                "symbol": row["symbol"],
                "name": row["name"],
                "exchange": row["exchange"],
                "sector_id": row["sector_id"],
                "sector_name": row["sector_name"],
                "close": close,
                "prior_close": prior,
                "change_pct": change_pct,
                "source": f"kr_candles_1d:{row['source']}",
                "as_of": _iso(row["bar_date"]),
            }
        )
    return result


def _breadth(
    bars: list[dict[str, Any]], day: date, coverage: dict[str, Any]
) -> dict[str, Any]:
    comparable = [row for row in bars if row["change_pct"] is not None]
    if not coverage["complete"]:
        return Evidence(
            None,
            "kr_candles_1d:KRX+kr_symbol_universe",
            day.isoformat(),
            "session_bar_coverage_incomplete",
            coverage,
        ).to_dict()
    if not comparable:
        return Evidence(
            None, None, None, "session_breadth_bars_missing", coverage
        ).to_dict()
    counts = {
        "advancers": sum(row["change_pct"] > 0 for row in comparable),
        "decliners": sum(row["change_pct"] < 0 for row in comparable),
        "unchanged": sum(row["change_pct"] == 0 for row in comparable),
        "universe": "active common shares with comparable KRX daily bars",
    }
    return Evidence(
        counts,
        "kr_candles_1d:KRX+kr_symbol_universe",
        day.isoformat(),
        coverage=coverage,
    ).to_dict()


def _sectors(
    bars: list[dict[str, Any]], day: date, coverage: dict[str, Any]
) -> dict[str, Any]:
    by_sector: dict[str, list[Decimal]] = defaultdict(list)
    for row in bars:
        if row["sector_name"] and row["change_pct"] is not None:
            by_sector[row["sector_name"]].append(row["change_pct"])
    if not coverage["complete"]:
        return Evidence(
            None,
            "kr_candles_1d:KRX+symbol_sectors",
            day.isoformat(),
            "session_bar_coverage_incomplete",
            coverage,
        ).to_dict()
    if not by_sector:
        return Evidence(None, None, None, "sector_bars_missing", coverage).to_dict()
    values = [
        {
            "sector": name,
            "equal_weighted_change_pct": str(sum(changes) / len(changes)),
            "constituents": len(changes),
        }
        for name, changes in sorted(by_sector.items())
    ]
    return Evidence(
        values,
        "kr_candles_1d:KRX+symbol_sectors",
        day.isoformat(),
        coverage=coverage,
    ).to_dict()


def _top_movers(
    bars: list[dict[str, Any]], day: date, coverage: dict[str, Any]
) -> dict[str, Any]:
    movers = _ordered_movers(bars)
    value = [
        {
            "symbol": row["symbol"],
            "name": row["name"],
            "close": str(row["close"]) if row["close"] is not None else None,
            "change_pct": str(row["change_pct"]),
            "source": row["source"],
            "as_of": row["as_of"],
        }
        for row in movers[:10]
    ]
    if not coverage["complete"]:
        return Evidence(
            None,
            "kr_candles_1d:KRX+kr_symbol_universe",
            day.isoformat(),
            "session_bar_coverage_incomplete",
            coverage,
        ).to_dict()
    if not value:
        return Evidence(None, None, None, "session_movers_missing", coverage).to_dict()
    return Evidence(
        value,
        "kr_candles_1d:KRX+kr_symbol_universe",
        day.isoformat(),
        coverage=coverage,
    ).to_dict()


def _ordered_movers(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    movers = [row for row in bars if row["change_pct"] is not None]
    movers.sort(key=lambda row: (-abs(row["change_pct"]), row["symbol"]))
    return movers


def _top_mover_symbols(bars: list[dict[str, Any]]) -> list[str]:
    return [row["symbol"] for row in _ordered_movers(bars)[:10]]


async def _load_disclosures(
    session: AsyncSession, day: date, symbols: list[str]
) -> tuple[dict[str, Any], bool]:
    partition = await session.scalar(
        select(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.source == "dart",
            MarketEventIngestionPartition.category == "disclosure",
            MarketEventIngestionPartition.market == "kr",
            MarketEventIngestionPartition.partition_date == day,
        )
    )
    complete_after = datetime.combine(day, DART_POST_CLOSE_COMPLETE_AT, KST)
    disclosures_complete = bool(
        partition is not None
        and partition.status == "succeeded"
        and partition.finished_at is not None
        and partition.finished_at >= complete_after
    )
    if not disclosures_complete:
        reason = (
            "disclosure_ingestion_failed"
            if partition is not None and partition.status == "failed"
            else "disclosure_ingestion_pending"
        )
        return (
            Evidence(
                None,
                "market_event_ingestion_partitions:dart:disclosure:kr",
                _iso(partition.finished_at) if partition is not None else None,
                reason,
            ).to_dict(),
            False,
        )
    if not symbols:
        return Evidence(None, None, None, "no_session_movers_to_link").to_dict(), True
    rows = (
        await session.scalars(
            select(MarketEvent)
            .where(
                MarketEvent.market == "kr",
                MarketEvent.source == "dart",
                MarketEvent.category.in_(("disclosure", "earnings")),
                MarketEvent.event_date == day,
                MarketEvent.symbol.in_(symbols),
            )
            .order_by(MarketEvent.symbol.asc(), MarketEvent.created_at.asc())
        )
    ).all()
    values = [
        {
            "symbol": row.symbol,
            "title": row.title,
            "source": row.source,
            "url": row.source_url,
            "as_of": _iso(row.release_time_utc or row.first_received_at),
        }
        for row in rows
    ]
    return Evidence(
        values or None,
        "market_events:disclosure",
        day.isoformat(),
        None if values else "no_linked_disclosures_for_top_movers",
    ).to_dict(), True


async def _load_news(
    session: AsyncSession, day: date, symbols: list[str]
) -> dict[str, Any]:
    if not symbols:
        return Evidence(None, None, None, "no_session_movers_to_link").to_dict()
    start = datetime.combine(day, time.min, tzinfo=KST).astimezone(ZoneInfo("UTC"))
    end = start + timedelta(days=1)
    links = (
        select(
            NewsArticleRelatedSymbol.article_id.label("article_id"),
            NewsArticleRelatedSymbol.symbol.label("symbol"),
        )
        .where(
            NewsArticleRelatedSymbol.market == "kr",
            NewsArticleRelatedSymbol.symbol.in_(symbols),
        )
        .union_all(
            select(
                SymbolNewsRelevance.article_id.label("article_id"),
                SymbolNewsRelevance.symbol.label("symbol"),
            ).where(
                SymbolNewsRelevance.market == "kr",
                SymbolNewsRelevance.status != "excluded",
                SymbolNewsRelevance.symbol.in_(symbols),
            )
        )
    ).subquery()
    unique_links = select(links.c.article_id, links.c.symbol).distinct().subquery()
    rows = (
        await session.execute(
            select(NewsArticle, unique_links.c.symbol)
            .join(unique_links, unique_links.c.article_id == NewsArticle.id)
            .where(
                NewsArticle.market == "kr",
                NewsArticle.published_at >= start,
                NewsArticle.published_at < end,
            )
            .order_by(NewsArticle.published_at.desc(), NewsArticle.id.asc())
            .limit(50)
        )
    ).all()
    values = [
        {
            "symbol": symbol,
            "title": article.title,
            "source": article.feed_source or article.source,
            "url": article.url,
            "provider_published_at": _iso(article.published_at),
        }
        for article, symbol in rows
    ]
    return Evidence(
        values or None,
        "news_articles.published_at:provider_time",
        _iso(max((article.published_at for article, _ in rows), default=None)),
        None if values else "no_provider_timed_news_for_top_movers",
    ).to_dict()
