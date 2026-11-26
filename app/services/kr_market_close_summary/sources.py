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
from app.models.market_events import MarketEvent
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services.kr_market_close_summary.types import Evidence

KST = ZoneInfo("Asia/Seoul")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def confirmed_flow_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate only the persisted confirmed Naver daily series."""
    confirmed = [row for row in rows if row.get("source") == "naver_finance"]
    if not confirmed:
        return Evidence(None, None, None, "confirmed_daily_flow_missing").to_dict()
    fields = ("foreign_net", "institution_net", "individual_net")
    totals = {
        f"{field}_sampled_symbol_sum": (
            str(sum((int(row[field]) for row in confirmed), 0))
            if all(row.get(field) is not None for row in confirmed)
            else None
        )
        for field in fields
    }
    totals["symbol_snapshot_count"] = len(confirmed)
    totals["coverage_basis"] = (
        "available stored symbol snapshots; not a market-wide total"
    )
    missing = [field for field, value in totals.items() if value is None]
    as_of = max(
        (row.get("collected_at") for row in confirmed if row.get("collected_at")),
        default=None,
    )
    return Evidence(
        totals,
        "investor_flow_snapshots:naver_finance_confirmed_daily",
        _iso(as_of),
        "partial_confirmed_flow_fields" if missing else None,
    ).to_dict()


async def load_facts(session: AsyncSession, session_date: date) -> dict[str, Any]:
    flows = await _load_confirmed_flows(session, session_date)
    bars = await _load_session_bars(session, session_date)
    symbols = _top_mover_symbols(bars)
    return {
        "indices": await _load_indices(session, session_date),
        "investor_flows": flows,
        "market_breadth": _breadth(bars, session_date),
        "sectors": _sectors(bars, session_date),
        "top_movers": _top_movers(bars, session_date),
        "disclosures": await _load_disclosures(session, session_date, symbols),
        "news": await _load_news(session, session_date, symbols),
        "macro": Evidence(
            None,
            None,
            None,
            "no_confirmed_bok_rate_observation_stored",
        ).to_dict(),
    }


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


async def _load_confirmed_flows(session: AsyncSession, day: date) -> dict[str, Any]:
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
        ]
    )


async def _load_session_bars(session: AsyncSession, day: date) -> list[dict[str, Any]]:
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
                    WHERE bar_date < :session_date AND venue_rank = 1
                    ORDER BY symbol, bar_date DESC
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


def _breadth(bars: list[dict[str, Any]], day: date) -> dict[str, Any]:
    comparable = [row for row in bars if row["change_pct"] is not None]
    if not comparable:
        return Evidence(None, None, None, "session_breadth_bars_missing").to_dict()
    counts = {
        "advancers": sum(row["change_pct"] > 0 for row in comparable),
        "decliners": sum(row["change_pct"] < 0 for row in comparable),
        "unchanged": sum(row["change_pct"] == 0 for row in comparable),
        "universe": "active common shares with comparable KRX daily bars",
    }
    return Evidence(
        counts, "kr_candles_1d:KRX+kr_symbol_universe", day.isoformat()
    ).to_dict()


def _sectors(bars: list[dict[str, Any]], day: date) -> dict[str, Any]:
    by_sector: dict[str, list[Decimal]] = defaultdict(list)
    for row in bars:
        if row["sector_name"] and row["change_pct"] is not None:
            by_sector[row["sector_name"]].append(row["change_pct"])
    if not by_sector:
        return Evidence(None, None, None, "sector_bars_missing").to_dict()
    values = [
        {
            "sector": name,
            "equal_weighted_change_pct": str(sum(changes) / len(changes)),
            "constituents": len(changes),
        }
        for name, changes in sorted(by_sector.items())
    ]
    return Evidence(
        values, "kr_candles_1d:KRX+symbol_sectors", day.isoformat()
    ).to_dict()


def _top_movers(bars: list[dict[str, Any]], day: date) -> dict[str, Any]:
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
    if not value:
        return Evidence(None, None, None, "session_movers_missing").to_dict()
    return Evidence(
        value, "kr_candles_1d:KRX+kr_symbol_universe", day.isoformat()
    ).to_dict()


def _ordered_movers(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    movers = [row for row in bars if row["change_pct"] is not None]
    movers.sort(key=lambda row: (-abs(row["change_pct"]), row["symbol"]))
    return movers


def _top_mover_symbols(bars: list[dict[str, Any]]) -> list[str]:
    return [row["symbol"] for row in _ordered_movers(bars)[:10]]


async def _load_disclosures(
    session: AsyncSession, day: date, symbols: list[str]
) -> dict[str, Any]:
    if not symbols:
        return Evidence(None, None, None, "no_session_movers_to_link").to_dict()
    rows = (
        await session.scalars(
            select(MarketEvent)
            .where(
                MarketEvent.market == "kr",
                MarketEvent.category == "disclosure",
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
    ).to_dict()


async def _load_news(
    session: AsyncSession, day: date, symbols: list[str]
) -> dict[str, Any]:
    if not symbols:
        return Evidence(None, None, None, "no_session_movers_to_link").to_dict()
    start = datetime.combine(day, time.min, tzinfo=KST).astimezone(ZoneInfo("UTC"))
    end = start + timedelta(days=1)
    rows = (
        await session.execute(
            select(NewsArticle, NewsArticleRelatedSymbol.symbol)
            .join(
                NewsArticleRelatedSymbol,
                NewsArticleRelatedSymbol.article_id == NewsArticle.id,
            )
            .where(
                NewsArticle.market == "kr",
                NewsArticleRelatedSymbol.market == "kr",
                NewsArticleRelatedSymbol.symbol.in_(symbols),
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
