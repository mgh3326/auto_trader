"""ROB-1303 read-only material assembly.

The only impure module in the package: plain ``SELECT``s against tables this
repo already fills. No new feed, no scraper, no credential, no write of any
kind.

Two things it refuses to do:

* **Guess a clock.** Stored ``article_published_at`` is naive, and the tz it was
  naive *in* depends on which ingestor produced the row. Only feeds in
  :data:`FEED_CLOCKS` with ``confirmed=True`` get an eligible timestamp;
  everything else is surfaced as ``timestamp_unknown`` and can never be a cause.
* **Fill a gap.** A material that could not be read is reported as unavailable
  with a reason, which is never the same statement as "no cause existed".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.spike_attribution.attribute import rule_eligibility
from app.services.spike_attribution.contract import (
    ELIGIBILITY_JUDGED_NOT_RELEVANT,
    ELIGIBILITY_TIMESTAMP_UNKNOWN,
    UNAVAILABLE_NO_COVERAGE,
    UNAVAILABLE_T_PLUS_1,
    DailyBar,
    EvidenceItem,
    MaterialAvailability,
    SpikeEvent,
    SpikeMaterials,
)

_KST = ZoneInfo("Asia/Seoul")

DAILY_TABLE_BY_MARKET: dict[str, str] = {"kr": "kr_candles_1d", "us": "us_candles_1d"}
SESSION_TZ_BY_MARKET: dict[str, ZoneInfo] = {
    "kr": _KST,
    "us": ZoneInfo("America/New_York"),
}


@dataclass(frozen=True)
class FeedClock:
    """How to read one feed's naive ``article_published_at``.

    ``confirmed`` is the gate: an unconfirmed clock yields ``timestamp_unknown``
    so the item is visible on the record but never counted as a cause.
    """

    tz: ZoneInfo | None
    precision: str  # "exact" | "date_only" | "unknown"
    confirmed: bool
    basis: str


# KST wall-clock, confirmed by the 2026-08 publish-hour histogram: the KR
# aggregate feed peaks 08:00-17:00 and empties overnight.
_KR_EXACT = FeedClock(
    tz=_KST,
    precision="exact",
    confirmed=True,
    basis="publish_hour_histogram_2026_08_kst_business_hours",
)
# 100% of these rows store 00:00:00 — the ingestor has the date only.
_KR_DATE_ONLY = FeedClock(
    tz=_KST,
    precision="date_only",
    confirmed=False,
    basis="all_rows_stored_at_midnight_time_of_day_not_captured",
)
# US feeds look like naive UTC, but "looks like" is not a confirmation and a
# 9-13 hour error would silently flip pre-move / post-move rulings.
_US_UNCONFIRMED = FeedClock(
    tz=None,
    precision="unknown",
    confirmed=False,
    basis="tz_not_confirmed_pending_ingestor_side_verification",
)

FEED_CLOCKS: dict[str, FeedClock] = {
    "http_naver_stock_aggregate": _KR_EXACT,
    "browser_naver_research": _KR_DATE_ONLY,
    "browser_naver_research_company": _KR_DATE_ONLY,
    "browser_naver_research_economy": _KR_DATE_ONLY,
    "browser_naver_research_industry": _KR_DATE_ONLY,
    "browser_naver_research_invest": _KR_DATE_ONLY,
    "naver_item_news": _KR_DATE_ONLY,
    "rss_yahoo_finance_topstories": _US_UNCONFIRMED,
    "rss_cnbc_us_markets": _US_UNCONFIRMED,
    "rss_cnbc_earnings": _US_UNCONFIRMED,
    "rss_cnbc_finance": _US_UNCONFIRMED,
    "rss_marketwatch_topstories": _US_UNCONFIRMED,
    "rss_fed_press": _US_UNCONFIRMED,
    "finnhub_company_news": _US_UNCONFIRMED,
    "finnhub_general_news": _US_UNCONFIRMED,
}

_UNREGISTERED = FeedClock(
    tz=None,
    precision="unknown",
    confirmed=False,
    basis="feed_source_not_in_clock_registry",
)

# DART receipt timestamps. The ROB-128 normalizer drops release_time_local, but
# raw_payload_json.rcept_dt keeps the real KST receipt time.
_DART_TIME_KEY = "rcept_dt"

# ROB-491 symbol_news_relevance.status → our judgment vocabulary. ``pending``
# deliberately maps to ``unjudged``: a queued row is not a verdict.
JUDGMENT_BY_STATUS: dict[str | None, str] = {
    "confirmed": "judged_relevant",
    "excluded": "judged_not_relevant",
    "pending": "unjudged",
}
EXTERNALLY_JUDGED_STATUSES: frozenset[str] = frozenset({"confirmed", "excluded"})


def feed_clock(feed_source: str | None) -> FeedClock:
    return FEED_CLOCKS.get(feed_source or "", _UNREGISTERED)


def _localize(naive: dt.datetime | None, clock: FeedClock) -> dt.datetime | None:
    if naive is None or clock.tz is None or not clock.confirmed:
        return None
    return naive.replace(tzinfo=clock.tz)


async def load_daily_bars(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    start: dt.date,
    end: dt.date,
) -> list[DailyBar]:
    """Ascending daily bars for ``symbol`` in ``[start, end]`` local dates."""

    table = DAILY_TABLE_BY_MARKET.get(market)
    if table is None:
        raise ValueError(f"unsupported market: {market!r}")
    tzname = str(SESSION_TZ_BY_MARKET[market])
    stmt = sa.text(
        f"""
        SELECT (time AT TIME ZONE :tz)::date AS session_date,
               open, high, low, close, volume
        FROM {table}
        WHERE symbol = :symbol
          AND (time AT TIME ZONE :tz)::date BETWEEN :start AND :end
        ORDER BY time
        """  # noqa: S608 - table name comes from a closed literal mapping
    )
    rows = await db.execute(
        stmt, {"tz": tzname, "symbol": symbol, "start": start, "end": end}
    )
    return [
        DailyBar(
            symbol=symbol,
            session_date=row.session_date,
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume if row.volume is not None else 0)),
        )
        for row in rows
    ]


async def _load_news_evidence(
    db: AsyncSession, event: SpikeEvent
) -> tuple[list[EvidenceItem], MaterialAvailability]:
    # Fetch a day either side of the window so post-move items are visible on
    # the record as ``after_move`` rather than quietly missing.
    lo = event.window_start_exclusive.date() - dt.timedelta(days=1)
    hi = event.window_end_inclusive.date() + dt.timedelta(days=1)
    stmt = sa.text(
        """
        SELECT a.id           AS article_id,
               a.url          AS url,
               a.title        AS title,
               a.source       AS source,
               a.feed_source  AS feed_source,
               a.article_published_at AS published_at,
               r.status       AS rel_status,
               r.relationship AS rel_relationship,
               r.relevance    AS rel_relevance,
               r.price_relevance AS rel_price_relevance,
               r.reason       AS rel_reason,
               s.source       AS map_source,
               s.score        AS map_score
        FROM news_article_related_symbols s
        JOIN news_articles a ON a.id = s.article_id
        LEFT JOIN symbol_news_relevance r
               ON r.article_id = a.id
              AND r.market = s.market
              AND r.symbol = s.symbol
        WHERE s.market = :market
          AND s.symbol = :symbol
          AND a.article_published_at >= :lo
          AND a.article_published_at < :hi
        ORDER BY a.article_published_at DESC, a.id DESC
        """
    )
    rows = (
        await db.execute(
            stmt,
            {
                "market": event.market,
                "symbol": event.symbol,
                "lo": dt.datetime.combine(lo, dt.time.min),
                "hi": dt.datetime.combine(hi, dt.time.max),
            },
        )
    ).all()

    items: list[EvidenceItem] = []
    for row in rows:
        clock = feed_clock(row.feed_source)
        published = _localize(row.published_at, clock)
        eligibility = (
            rule_eligibility(
                published_at=published,
                window_start_exclusive=event.window_start_exclusive,
                window_end_inclusive=event.window_end_inclusive,
            )
            if published is not None
            else ELIGIBILITY_TIMESTAMP_UNKNOWN
        )
        # ROB-491 status vocabulary: confirmed / pending / excluded, written
        # only by the external judgment job. This code never sets any of them.
        #   confirmed → the judge tied the article to this symbol
        #   excluded  → the judge ruled it unrelated (or low relevance)
        #   pending / no row → nobody has looked yet
        # A pending row is *not* a judgment, so it must not read as one, and an
        # excluded row must not become a cause just because it landed in the
        # window. Both stay visible with their reason.
        judgment = JUDGMENT_BY_STATUS.get(row.rel_status, "unjudged")
        if judgment == "judged_not_relevant":
            eligibility = ELIGIBILITY_JUDGED_NOT_RELEVANT
        items.append(
            EvidenceItem(
                attribution_type="news",
                source=row.feed_source or row.source or "news",
                title=row.title,
                url=row.url,
                published_at=published,
                published_at_precision=clock.precision,
                published_at_source=(
                    f"news_articles.article_published_at ({clock.basis})"
                ),
                eligibility=eligibility,
                judgment=judgment,
                judgment_detail={
                    "status": row.rel_status,
                    "relationship": row.rel_relationship,
                    "relevance": row.rel_relevance,
                    "price_relevance": row.rel_price_relevance,
                    "reason": row.rel_reason,
                    "judged_by_external_job": row.rel_status
                    in EXTERNALLY_JUDGED_STATUSES,
                },
                ref={
                    "article_id": row.article_id,
                    "mapping_source": row.map_source,
                    "mapping_score": (
                        float(row.map_score) if row.map_score is not None else None
                    ),
                    "feed_clock_confirmed": clock.confirmed,
                },
            )
        )
    availability = MaterialAvailability(
        material="news",
        available=True,
        reason=None,
        detail={
            "rows": len(items),
            "eligible": sum(item.is_eligible for item in items),
            "auto_exclusion_by_this_code": False,
        },
    )
    return items, availability


async def _company_names(
    db: AsyncSession, *, market: str, symbol: str
) -> tuple[str, ...]:
    if market != "kr":
        return ()
    rows = await db.execute(
        sa.text("SELECT name FROM kr_symbol_universe WHERE symbol = :symbol"),
        {"symbol": symbol},
    )
    return tuple(row.name for row in rows if row.name)


async def _load_market_event_evidence(
    db: AsyncSession, event: SpikeEvent
) -> tuple[list[EvidenceItem], list[MaterialAvailability]]:
    names = await _company_names(db, market=event.market, symbol=event.symbol)
    lo = event.window_start_exclusive.date()
    hi = event.window_end_inclusive.date()
    stmt = sa.text(
        """
        SELECT id, category, title, company_name, symbol, event_date,
               source, source_url, raw_payload_json
        FROM market_events
        WHERE event_date BETWEEN :lo AND :hi
          AND market = :market
          AND (
                symbol = :symbol
             OR (
                  symbol IS NULL
                  AND company_name = ANY(CAST(:names AS text[]))
                )
          )
        ORDER BY event_date DESC, id DESC
        """
    )
    rows = (
        await db.execute(
            stmt,
            {
                "lo": lo,
                "hi": hi,
                "market": event.market,
                "symbol": event.symbol,
                "names": list(names),
            },
        )
    ).all()

    items: list[EvidenceItem] = []
    for row in rows:
        payload: dict[str, Any] = row.raw_payload_json or {}
        raw_time = str(payload.get(_DART_TIME_KEY) or "")
        published: dt.datetime | None = None
        precision = "unknown"
        time_source = "market_events.event_date (date only)"
        if len(raw_time) >= 19:
            try:
                published = dt.datetime.fromisoformat(raw_time).replace(tzinfo=_KST)
                precision = "exact"
                time_source = (
                    "market_events.raw_payload_json.rcept_dt "
                    "(KST; normalizer drops release_time_local)"
                )
            except ValueError:
                published = None
        eligibility = (
            rule_eligibility(
                published_at=published,
                window_start_exclusive=event.window_start_exclusive,
                window_end_inclusive=event.window_end_inclusive,
            )
            if published is not None
            else ELIGIBILITY_TIMESTAMP_UNKNOWN
        )
        attribution_type = "earnings" if row.category == "earnings" else "disclosure"
        items.append(
            EvidenceItem(
                attribution_type=attribution_type,
                source=f"{row.source}:{row.category}",
                title=row.title or payload.get("report_nm") or "(untitled)",
                url=row.source_url,
                published_at=published,
                published_at_precision=precision,
                published_at_source=time_source,
                eligibility=eligibility,
                judgment="not_applicable",
                judgment_detail={"filer": payload.get("flr_nm")},
                ref={
                    "market_event_id": row.id,
                    "company_name": row.company_name,
                    "symbol_column": row.symbol,
                    "linked_by": (
                        "symbol" if row.symbol else "company_name_exact_match"
                    ),
                },
            )
        )

    # KR disclosure needs a company-name to link on. Outside KR there is no
    # filing source ingested at all (market_events holds only finnhub earnings
    # for US), so reporting it "available" would overstate what was consulted.
    if event.market == "kr":
        disclosure_available = bool(names)
        disclosure_reason = None if names else UNAVAILABLE_NO_COVERAGE
        disclosure_note = (
            "market_events.symbol is NULL for every DART row in this database, "
            "so KR linkage is an exact company_name match and will miss a "
            "renamed or differently-spelled filer"
        )
    else:
        disclosure_available = False
        disclosure_reason = UNAVAILABLE_NO_COVERAGE
        disclosure_note = (
            f"no filing source is ingested for market={event.market!r}; "
            "market_events carries earnings only"
        )

    availability = [
        MaterialAvailability(
            material="disclosure",
            available=disclosure_available,
            reason=disclosure_reason,
            detail={
                "rows": sum(item.attribution_type == "disclosure" for item in items),
                "company_names_tried": list(names),
                "symbol_column_note": disclosure_note,
            },
        ),
        MaterialAvailability(
            material="earnings",
            available=True,
            reason=None,
            detail={
                "rows": sum(item.attribution_type == "earnings" for item in items),
                "time_note": (
                    "KR/DART earnings carry an exact rcept_dt. US/finnhub rows "
                    "carry only time_hint (before_open / after_close / "
                    "during_market), which orders the event against the session "
                    "but is not a clock — deriving eligibility from it is a "
                    "follow-up, not an assumed timestamp"
                ),
            },
        ),
    ]
    return items, availability


async def _flow_availability(
    db: AsyncSession, event: SpikeEvent
) -> MaterialAvailability:
    rows = (
        await db.execute(
            sa.text(
                """
                SELECT snapshot_date, foreign_net, institution_net,
                       individual_net, double_buy, collected_at
                FROM investor_flow_snapshots
                WHERE market = :market AND symbol = :symbol
                  AND snapshot_date <= :session_date
                ORDER BY snapshot_date DESC
                LIMIT 2
                """
            ),
            {
                "market": event.market,
                "symbol": event.symbol,
                "session_date": event.session_date,
            },
        )
    ).all()
    same_day = [row for row in rows if row.snapshot_date == event.session_date]
    return MaterialAvailability(
        material="flow",
        available=bool(same_day),
        reason=None if same_day else UNAVAILABLE_T_PLUS_1,
        detail={
            "same_day_snapshot": bool(same_day),
            "latest_snapshot_date": (
                rows[0].snapshot_date.isoformat() if rows else None
            ),
            "eligible_as_cause_in_v1": False,
            "context_rows": [
                {
                    "snapshot_date": row.snapshot_date.isoformat(),
                    "foreign_net": row.foreign_net,
                    "institution_net": row.institution_net,
                    "individual_net": row.individual_net,
                    "double_buy": row.double_buy,
                }
                for row in rows
            ],
        },
    )


async def _sector_availability(
    db: AsyncSession, event: SpikeEvent
) -> MaterialAvailability:
    if event.market != "kr":
        return MaterialAvailability(
            material="sector",
            available=False,
            reason=UNAVAILABLE_NO_COVERAGE,
            detail={"note": "US sector join not wired in this package"},
        )
    row = (
        await db.execute(
            sa.text(
                """
                SELECT u.sector_id, s.name_kr, s.name_en, s.source_key
                FROM kr_symbol_universe u
                LEFT JOIN symbol_sectors s ON s.id = u.sector_id
                WHERE u.symbol = :symbol
                """
            ),
            {"symbol": event.symbol},
        )
    ).first()
    has_sector = bool(row and row.sector_id)
    return MaterialAvailability(
        material="sector",
        available=has_sector,
        reason=None if has_sector else UNAVAILABLE_NO_COVERAGE,
        detail={
            "sector_id": row.sector_id if row else None,
            "name_kr": row.name_kr if row else None,
            "source_key": row.source_key if row else None,
            "eligible_as_cause_in_v1": False,
            "coverage_note": (
                "symbol_sectors is lazy-filled by screener enrichment, so a "
                "symbol that no screener has touched has no sector row"
            ),
        },
    )


async def load_spike_materials(db: AsyncSession, event: SpikeEvent) -> SpikeMaterials:
    """Assemble every material for one spike event. Read-only."""

    news_items, news_availability = await _load_news_evidence(db, event)
    event_items, event_availability = await _load_market_event_evidence(db, event)
    flow = await _flow_availability(db, event)
    sector = await _sector_availability(db, event)
    return SpikeMaterials(
        evidence=tuple(news_items + event_items),
        availability=(news_availability, *event_availability, flow, sector),
    )


__all__ = [
    "DAILY_TABLE_BY_MARKET",
    "EXTERNALLY_JUDGED_STATUSES",
    "FEED_CLOCKS",
    "JUDGMENT_BY_STATUS",
    "SESSION_TZ_BY_MARKET",
    "FeedClock",
    "feed_clock",
    "load_daily_bars",
    "load_spike_materials",
]
