"""hk 6145: closed 1-minute bars -> research minute tables, insert-only.

KR (venues KRX via KIS market ``J`` and NTX via ``NX``) goes to
``research.kr_candles_1m``; US (KIS overseas minute chart, 04:00-20:00 ET) goes
to ``research.us_candles_1m``. ``public.*_candles_1m`` are never written.

Rules (design hk:doc design/2026-11-24/at-session-timeline §4):

* KIS labels a minute bar by its START in the exchange's local clock (KST for
  KR, ET for US) and includes the still-forming current minute. A bar is kept
  only when ``bar_start + 1 min + 10 s <= observed_before`` where
  ``observed_before`` is read BEFORE the request goes out, so the stored bar
  can never be a partial one.
* ``retrieved_at`` is the first receipt: rows are ``INSERT ... ON CONFLICT DO
  NOTHING``; a re-run never touches an existing row, so its ``retrieved_at``
  stays.
* ``session_segment`` comes from ``session_segment_of`` (bar start, venue).
  An unknown segment is stored as ``UNKNOWN``, never guessed.
* No backfill, no scheduler: this module registers nothing.

US day market (hk 6322): when a Toss US market calendar is passed, US runs also
read Toss ``GET /api/v1/candles`` 1m bars (source ``TOSS``, venue
``TOSS_INTEGRATED``, one integrated feed). The calendar is fetched once per run
by the CLI. KIS US reads are skipped while a calendar day-market window is open,
because KIS has no bars there. Toss bars are labelled from the calendar alone
(``US_DAY`` inside dayMarket, ``UNKNOWN`` outside every window) and their 1m
timestamp is the bar END, so the bar start is ``timestamp - 1 min``.

The KIS client is injected and only four read methods are used (see
``KisMinuteReader``); the Toss reader only ``collect_minute_candles`` (see
``TossMinuteReader``). This module imports no order, ledger, approval or
broker-mutation code, and no Toss module at runtime.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import kst_day_window
from app.services.brokers.kis.live_order_expiry import (
    KR_PREMARKET_OPEN_KST,
    KR_REGULAR_OPEN_KST,
)
from app.services.market_events.session_calendar import is_trading_session
from app.services.research_candles.session_segment import (
    TOSS_US_VENUE,
    session_segment_of,
    us_calendar_segment,
    us_day_market_bar,
    us_session_date,
)
from app.services.research_candles.us_mover_score import (
    REASON_NOT_COMMON_OR_ETF,
    UsMoverScore,
    plain_decimal,
    score_us_movers,
)

if TYPE_CHECKING:  # type only
    from app.services.brokers.toss.dto import TossMinuteCandle
    from app.services.brokers.toss.market_calendar import TossMarketCalendar

logger = logging.getLogger(__name__)

Market = Literal["kr", "us"]

SOURCE = "KIS"
TOSS_SOURCE = "TOSS"
TOSS_USD = "USD"
BAR = timedelta(minutes=1)
CLOSE_GRACE = timedelta(seconds=10)
BATCH_PREFIX = "session-minutes"
#: Toss US lookback per symbol and run (hk 6322). The 5-minute cadence re-reads the
#: window; a re-read bar is ``DO NOTHING``. 3 h fits one 200-bar page.
TOSS_US_LOOKBACK = timedelta(hours=3)
TOSS_US_MAX_PAGES = 4
#: Toss ``before`` cursor is set this far past the clock, so the still-forming
#: minute (timestamp = its END, up to 1 min ahead) is returned and then dropped
#: by ``is_closed``, instead of tripping the client's "newer than before" check.
TOSS_CURSOR_SLACK = timedelta(minutes=2)
#: KIS keeps being read for this long after a day-market window opens. KIS after-hours
#: ends at the same instant the window opens (20:00 ET), so the last KIS bars (19:54-
#: 19:59 ET) are read by the first runs after the open, not dropped (review r1).
KIS_DAY_MARKET_GRACE = timedelta(minutes=15)
#: US extended session start: the backfill of a newly promoted US mover pages back
#: to this ET minute (hk 6510), and a page loop stops there.
US_SESSION_START_ET = time(4, 0)
US_BACKFILL_MAX_PAGES = 8
TOSS_VENUE_ERROR = TOSS_US_VENUE

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")

KR_TABLE = "research.kr_candles_1m"
US_TABLE = "research.us_candles_1m"
#: KIS market codes for the two KR venues.
KR_VENUE_MARKET_CODE = {"KRX": "J", "NTX": "NX"}
US_PICK_LOOKBACK = timedelta(hours=24)
_US_VENUES = frozenset({"NASD", "NYSE", "AMEX"})


class TossMinuteReader(Protocol):
    """The only Toss surface the collector may touch (read-only, hk 6322)."""

    async def collect_minute_candles(
        self,
        symbol: str,
        *,
        adjusted: bool,
        before: str,
        not_before: datetime,
        max_pages: int = 16,
        page_count: int = 200,
        pace: Any = None,
    ) -> list[TossMinuteCandle]: ...


class KisMinuteReader(Protocol):
    """The only KIS surface the collector may touch (all read-only)."""

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "J",
        time_unit: int = 1,
        n: int = 200,
        end_time: str | None = None,
    ) -> pd.DataFrame: ...

    async def inquire_overseas_minute_chart(
        self, symbol: str, exchange_code: str = "NASD", n: int = 120, keyb: str = ""
    ) -> Any: ...

    async def fetch_my_stocks(self) -> list[dict[str, Any]]: ...

    async def fetch_my_us_stocks(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class MinuteBar:
    time_utc: datetime  # bar START
    session_date: date  # KST date (kr) / ET trading day (us)
    symbol: str
    venue: str
    session_segment: str
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal | None  # KR: NULL when the per-minute value cannot be proven


@dataclass(slots=True)
class FrameResult:
    bars: list[MinuteBar] = field(default_factory=list)
    skipped_forming: int = 0
    skipped_invalid: int = 0
    null_value: int = 0  # KR bars kept with a NULL value


#: First minute of a KR venue's day: its accumulated value starts from zero, so
#: the accumulated value itself is that bar's value.
_KR_DAY_FIRST_MINUTE = {"KRX": KR_REGULAR_OPEN_KST, "NTX": KR_PREMARKET_OPEN_KST}


def is_closed(bar_start_utc: datetime, observed_before: datetime) -> bool:
    """True once the minute is over by at least the 10 s grace."""
    return bar_start_utc + BAR + CLOSE_GRACE <= observed_before


def _decimal(value: object) -> Decimal | None:
    try:
        if value is None or pd.isna(value):
            return None
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def bars_from_frame(
    frame: pd.DataFrame,
    *,
    market: Market,
    symbol: str,
    venue: str,
    observed_before: datetime,
    us_calendar: TossMarketCalendar | None = None,
) -> FrameResult:
    """KIS frame (``datetime`` = naive START label in the local clock) -> bars.

    KR ``value`` (``acml_tr_pbmn``) is the ACCUMULATED trade value of the day,
    not the per-minute amount (``volume`` is per minute). The per-minute value is
    stored only when it is proven:

    * the previous row of the same KST day is in the frame -> the difference, or
    * the bar is exactly the venue's first minute of the day (KRX 09:00,
      NTX 08:00) -> the accumulated value itself.

    Anything else is stored as NULL — the bar is kept, the value is not guessed
    (unknown is neither 0 nor an estimate; frame length is never used to infer
    that the day start was reached). A row whose accumulated value went down, the
    row after it, and the row after a row dropped as invalid are NULL too, because
    the baseline (or a dropped minute's trade value) would otherwise be folded
    into the difference. US ``value`` is per bar and stored as is.
    """
    result = FrameResult()
    if frame is None or frame.empty:
        return result
    local_tz = _KST if market == "kr" else _ET
    parsed_rows: dict[datetime, list[Decimal]] = {}
    invalid_starts: list[datetime] = []
    records = frame.to_dict("records")
    for item in records:
        parsed = pd.to_datetime(item.get("datetime"), errors="coerce")
        if pd.isna(parsed):
            result.skipped_invalid += 1
            continue
        stamp = pd.Timestamp(parsed)
        stamp = (
            stamp.tz_localize(local_tz)
            if stamp.tzinfo is None
            else stamp.tz_convert(local_tz)
        )
        start_utc = (
            stamp.to_pydatetime().astimezone(UTC).replace(second=0, microsecond=0)
        )
        if start_utc in parsed_rows:
            continue
        values = [
            _decimal(item.get(key))
            for key in ("open", "high", "low", "close", "volume", "value")
        ]
        if any(v is None for v in values[:5]) or (values[5] is None and market != "kr"):
            result.skipped_invalid += 1
            invalid_starts.append(start_utc)
            continue
        if values[5] is None:
            # KR bar is usable, its accumulated value is not: acts as a chain break
            invalid_starts.append(start_utc)
        parsed_rows[start_utc] = values  # type: ignore[assignment]

    previous: tuple[datetime, Decimal] | None = None
    suspect_next = False
    for start_utc in sorted(parsed_rows):
        o, h, low, c, vol, value = parsed_rows[start_utc]
        if market == "kr":
            local = start_utc.astimezone(_KST)
            accumulated, value = value, None
            same_day_previous = (
                previous is not None
                and previous[0].astimezone(_KST).date() == local.date()
            )
            lower = previous[0] if same_day_previous and previous else None
            dropped_between = any(
                dropped < start_utc
                and dropped.astimezone(_KST).date() == local.date()
                and (lower is None or dropped > lower)
                for dropped in invalid_starts
            )
            if not same_day_previous:
                suspect_next = False
            decreased = False
            if not dropped_between and not suspect_next and accumulated is not None:
                if same_day_previous and previous is not None:
                    if accumulated >= previous[1]:
                        value = accumulated - previous[1]
                    else:
                        decreased = True
                elif local.time() == _KR_DAY_FIRST_MINUTE.get(venue):
                    value = accumulated
            suspect_next = decreased
            previous = (start_utc, accumulated) if accumulated is not None else previous
            if value is None and is_closed(start_utc, observed_before):
                result.null_value += 1
        if not is_closed(start_utc, observed_before):
            result.skipped_forming += 1
            continue
        result.bars.append(
            MinuteBar(
                time_utc=start_utc,
                session_date=(
                    start_utc.astimezone(_KST).date()
                    if market == "kr"
                    else us_session_date(start_utc)
                ),
                symbol=symbol,
                venue=venue,
                session_segment=session_segment_of(
                    market, venue, start_utc, us_calendar=us_calendar
                ),
                source=SOURCE,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=vol,
                value=value,
            )
        )
    return result


def bars_from_toss_candles(
    candles: Iterable[TossMinuteCandle],
    *,
    symbol: str,
    observed_before: datetime,
    us_calendar: TossMarketCalendar | None,
) -> FrameResult:
    """Toss 1m candles (timestamp = bar END) -> closed US bars (hk 6322).

    The bar START is ``timestamp - 1 min``. A bar is kept only when it is closed
    by the same rule as KIS (``is_closed``), so the still-forming minute is counted
    in ``skipped_forming`` and never stored. A candle whose currency is not USD is
    counted in ``skipped_invalid``. The label comes from the calendar only:
    ``US_DAY`` inside dayMarket, the calendar's pre/regular/post window otherwise,
    ``UNKNOWN`` when no window holds the bar (stored, not guessed). Toss does not
    report a per-minute trade value, so ``value`` is NULL.
    """
    result = FrameResult()
    for candle in candles:
        start_utc = candle.bar_start.astimezone(UTC)
        if candle.currency != TOSS_USD:
            result.skipped_invalid += 1
            continue
        if not is_closed(start_utc, observed_before):
            result.skipped_forming += 1
            continue
        result.bars.append(
            MinuteBar(
                time_utc=start_utc,
                session_date=us_session_date(start_utc),
                symbol=symbol,
                venue=TOSS_US_VENUE,
                session_segment=session_segment_of(
                    "us", TOSS_US_VENUE, start_utc, us_calendar=us_calendar
                ),
                source=TOSS_SOURCE,
                open=candle.open_price,
                high=candle.high_price,
                low=candle.low_price,
                close=candle.close_price,
                volume=candle.volume,
                value=None,
            )
        )
    return result


def calendar_overlaps(
    calendar: TossMarketCalendar | None, start: datetime, end: datetime
) -> bool:
    """True when any Toss US window of the calendar overlaps ``[start, end)``."""
    if calendar is None:
        return False
    for day in calendar.days:
        for attribute in ("day_market", "pre_market", "regular_market", "after_market"):
            window = getattr(day, attribute, None)
            if window is not None and window.start < end and start < window.end:
                return True
    return False


# --------------------------------------------------------------------- writer


def _auction_window(observed_before: datetime) -> tuple[time, time] | None:
    local = observed_before.astimezone(_KST)
    clock = local.time()
    if time(9, 5) <= clock < time(9, 30):
        window = (time(8, 59), time(9))
    elif time(15, 35) <= clock < time(16):
        window = (time(15, 19), time(15, 30))
    else:
        return None
    return window if is_trading_session("kr", local.date()) else None


class MinuteWriter(Protocol):
    async def insert(
        self,
        bars: Sequence[MinuteBar],
        *,
        batch_id: str,
        retrieved_at: datetime,
        auction_observed_before: datetime | None = None,
    ) -> tuple[int, int]:
        """Return ``(inserted, already_present)``."""
        ...


_KR_INSERT = text(
    f"""
    INSERT INTO {KR_TABLE}
        (time_utc, session_date_kst, symbol, venue, session_segment, source,
         open, high, low, close, volume, value, retrieved_at, batch_id)
    VALUES
        (:time_utc, :session_date, :symbol, :venue, :session_segment, :source,
         :open, :high, :low, :close, :volume, :value, :retrieved_at, :batch_id)
    ON CONFLICT (time_utc, symbol, venue) DO NOTHING
    """
)
_US_INSERT = text(
    f"""
    INSERT INTO {US_TABLE}
        (time_utc, session_date, symbol, venue, session_segment, source,
         open, high, low, close, volume, value, retrieved_at, batch_id)
    VALUES
        (:time_utc, :session_date, :symbol, :venue, :session_segment, :source,
         :open, :high, :low, :close, :volume, :value, :retrieved_at, :batch_id)
    ON CONFLICT (time_utc, symbol, venue) DO NOTHING
    """
)


_KR_AUCTION_REVISION = text("""
    INSERT INTO research.kr_auction_minute_revisions
        (time_utc, symbol, venue, open, high, low, close, volume, value, revised_at)
    SELECT time_utc, symbol, venue, :open, :high, :low, :close, :volume, :value, :revised_at
    FROM research.kr_candles_1m
    WHERE (time_utc, symbol, venue) = (:time_utc, :symbol, :venue)
      AND retrieved_at < :observed_before
      AND batch_id NOT ILIKE '%backfill%' AND batch_id NOT ILIKE 'promote-%'
    ON CONFLICT (time_utc, symbol, venue) DO NOTHING
""")


class SqlMinuteWriter:
    """Append first receipts and eligible revisions in one symbol/venue transaction."""

    def __init__(self, session: AsyncSession, market: Market) -> None:
        self._session = session
        self._market = market
        self._statement = _KR_INSERT if market == "kr" else _US_INSERT

    async def insert_one(
        self,
        bar: MinuteBar,
        *,
        batch_id: str,
        retrieved_at: datetime,
    ) -> bool:
        """Insert one bar and return whether a row was created.

        No commit: the caller owns the transaction (hk 6343's HTTP ingest wraps
        each bar in its own savepoint so one refused row cannot discard the
        rest of the batch).
        """
        outcome = await self._session.execute(
            self._statement,
            {
                "time_utc": bar.time_utc,
                "session_date": bar.session_date,
                "symbol": bar.symbol,
                "venue": bar.venue,
                "session_segment": bar.session_segment,
                "source": bar.source,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "value": bar.value,
                "retrieved_at": retrieved_at,
                "batch_id": batch_id,
            },
        )
        return (getattr(outcome, "rowcount", 0) or 0) > 0

    async def insert(
        self,
        bars: Sequence[MinuteBar],
        *,
        batch_id: str,
        retrieved_at: datetime,
        auction_observed_before: datetime | None = None,
    ) -> tuple[int, int]:
        window = (
            _auction_window(auction_observed_before)
            if self._market == "kr" and auction_observed_before is not None
            else None
        )
        inserted = 0
        try:
            for bar in bars:
                created = await self.insert_one(
                    bar, batch_id=batch_id, retrieved_at=retrieved_at
                )
                inserted += 1 if created else 0
                if (
                    not created
                    and window is not None
                    and auction_observed_before is not None
                    and bar.venue == "KRX"
                    and bar.source == SOURCE
                    and bar.volume > 0
                    and bar.session_date
                    == auction_observed_before.astimezone(_KST).date()
                    and window[0] <= bar.time_utc.astimezone(_KST).time() <= window[1]
                ):
                    await self._session.execute(
                        _KR_AUCTION_REVISION,
                        {
                            "time_utc": bar.time_utc,
                            "symbol": bar.symbol,
                            "venue": bar.venue,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "value": bar.value,
                            "observed_before": auction_observed_before,
                            "revised_at": retrieved_at,
                        },
                    )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return inserted, len(bars) - inserted


# -------------------------------------------------------------------- targets


def normalize_symbol(market: Market, value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if market == "kr":
        raw = raw.zfill(6) if len(raw) < 6 else raw
        return raw if len(raw) == 6 and raw.isalnum() else None
    return to_db_symbol(raw) or None


def parse_symbol_list(market: Market, raw: str | None) -> list[str]:
    symbols = (normalize_symbol(market, part) for part in (raw or "").split(","))
    return sorted({s for s in symbols if s})


#: Origin order of the holdings / fixed / movers / screener / pick-log candidates
#: (report key order only; selection is by tier, see ``TIER_ORDER``).
TARGET_ORIGIN_ORDER = (
    "kis_holdings",
    "manual_holdings",
    "fixed_list",
    "intraday_movers",
    "screener_snapshot",
    "screener_pick_log",
)
#: Selection tiers, highest priority first (hk 6299). ``fixed`` = holdings (KIS and
#: manual) + the fixed list: never cut. ``intraday_movers`` = KR intraday promotion
#: (today's 6154 first captures). ``screener_snapshot`` = pre-open screen top N.
#: ``screener_pick_log`` = the remainder.
TIER_FIXED = "fixed"
TIER_ORDER = (TIER_FIXED, "intraday_movers", "screener_snapshot", "screener_pick_log")
CUT_REASON_BUDGET = "budget_cut"
DEFAULT_SCREENER_TOP_N = 20
DEFAULT_MAX_SYMBOLS = 150
DEFAULT_FIXED_BUDGET = 50
DEFAULT_INTRADAY_BUDGET = 40
DEFAULT_SCREENER_BUDGET = 60
#: hk 6554: US movers below this dollar volume rank after the ones at or above it
#: (a bucket, not an exclusion). A default, to be re-checked on real dry-run data.
DEFAULT_US_MOVER_DOLLAR_VOLUME_FLOOR = Decimal("100000")
MOVERS_TIER = "intraday_movers"


@dataclass(frozen=True, slots=True)
class TierSpec:
    """One selection tier. ``symbols`` are in priority order (may repeat).

    ``budget`` None = no per-tier budget (the run cap still applies). ``exempt``
    tiers are never cut; their unused budget still spills to the next tier.
    ``protected`` symbols are kept ahead of the tier budget (not ahead of the run
    cap). Only protected symbols that are still candidates of this tier count, so a
    symbol a higher tier already took cannot raise this tier's allowance.
    """

    tier: str
    symbols: Sequence[str]
    budget: int | None
    exempt: bool = False
    protected: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TierReport:
    tier: str
    budget: int | None  # base budget
    allowance: (
        int | None
    )  # base + spill-over, before the run cap; None = unbudgeted/exempt
    candidates: int  # not already taken by a higher tier
    selected: int
    cut: int


@dataclass(frozen=True, slots=True)
class TieredSelection:
    selected: list[str]  # in priority order
    tiers: list[TierReport]
    cuts: list[dict[str, str]]  # {"symbol", "tier", "reason"} for symbols left out
    truncated: dict[str, int]  # cut count per tier
    taken_by: dict[str, str]  # selected symbol -> the tier that took it (hk 6554)


def select_tiered(tiers: Sequence[TierSpec], max_symbols: int) -> TieredSelection:
    """Pick symbols tier by tier under a per-tier budget and one run cap.

    Unused budget of a tier (``budget + carry - taken``) carries to the next tier.
    A symbol that a tier does not take stays eligible for the lower tiers and is
    reported as cut only when no tier takes it; the cut reason is the first tier
    that left it out. Exempt tiers consume the run cap but are never trimmed by it.
    """
    remaining = max(int(max_symbols), 0)
    carry = 0
    chosen: list[str] = []
    chosen_set: set[str] = set()
    cut_by: dict[str, str] = {}
    cut_order: list[str] = []
    reports: list[tuple[TierSpec, list[str], list[str], int | None]] = []
    for spec in tiers:
        listed: set[str] = set()
        open_symbols: list[str] = []
        for symbol in spec.symbols:
            if symbol not in chosen_set and symbol not in listed:
                listed.add(symbol)
                open_symbols.append(symbol)
        # protected candidates first (stable), so the prefix slice keeps them
        candidates = [s for s in open_symbols if s in spec.protected] + [
            s for s in open_symbols if s not in spec.protected
        ]
        kept_count = sum(1 for s in candidates if s in spec.protected)
        pool = None if spec.budget is None else spec.budget + carry
        if spec.exempt:
            take = candidates
        else:
            limit = remaining if pool is None else min(max(pool, kept_count), remaining)
            take = candidates[:limit]
        taken = set(take)
        cut = [symbol for symbol in candidates if symbol not in taken]
        for symbol in cut:
            if symbol not in cut_by:
                cut_by[symbol] = spec.tier
                cut_order.append(symbol)
        chosen.extend(take)
        chosen_set.update(take)
        remaining = max(remaining - len(take), 0)
        carry = 0 if pool is None else max(pool - len(take), 0)
        reports.append((spec, candidates, take, pool))

    cuts = [
        {"symbol": symbol, "tier": cut_by[symbol], "reason": CUT_REASON_BUDGET}
        for symbol in cut_order
        if symbol not in chosen_set
    ]
    cut_counts: dict[str, int] = {}
    for item in cuts:
        cut_counts[item["tier"]] = cut_counts.get(item["tier"], 0) + 1
    taken_by = {symbol: spec.tier for spec, _, take, _ in reports for symbol in take}
    tier_reports = [
        TierReport(
            tier=spec.tier,
            budget=spec.budget,
            allowance=None if (spec.exempt or pool is None) else pool,
            candidates=len(candidates),
            selected=len(take),
            cut=cut_counts.get(spec.tier, 0),
        )
        for spec, candidates, take, pool in reports
    ]
    return TieredSelection(
        selected=chosen,
        tiers=tier_reports,
        cuts=cuts,
        truncated={k: v for k, v in cut_counts.items() if v},
        taken_by=taken_by,
    )


def select_with_cap(
    tiers: Sequence[tuple[str, Sequence[str]]], max_symbols: int
) -> tuple[list[str], dict[str, int]]:
    """Cap-only selection in the given order (no per-tier budgets).

    Returns ``(kept_in_priority_order, truncated_count_by_origin)``.
    """
    selection = select_tiered(
        [TierSpec(origin, symbols, budget=None) for origin, symbols in tiers],
        max_symbols,
    )
    return selection.selected, selection.truncated


@dataclass(slots=True)
class TargetSet:
    symbols: list[str]
    by_origin: dict[str, int]
    errors: list[dict[str, str]] = field(default_factory=list)
    truncated: dict[str, int] = field(default_factory=dict)
    #: KR symbols first seen as intraday movers that have no minute row yet today:
    #: they get one extra, earlier KIS page (hk 6154). Subset of ``symbols``.
    backfill: list[str] = field(default_factory=list)
    tiers: list[TierReport] = field(default_factory=list)
    cuts: list[dict[str, str]] = field(default_factory=list)
    #: US only (hk 6554): the per-symbol decision record of this run. None for KR,
    #: so the KR report has no such key and its bytes are unchanged.
    decisions: dict[str, Any] | None = None


def targets_report(targets: TargetSet) -> dict[str, Any]:
    """The per-run target fields of the collector JSON (hk 6299)."""
    return {
        "targeted_symbols": list(targets.symbols),
        "targets_by_origin": dict(targets.by_origin),
        "targets_truncated": dict(targets.truncated),
        "targets_truncated_total": sum(targets.truncated.values()),
        "targets_tiers": [
            {
                "tier": t.tier,
                "budget": t.budget,
                "allowance": t.allowance,
                "candidates": t.candidates,
                "selected": t.selected,
                "cut": t.cut,
            }
            for t in targets.tiers
        ],
        "targets_cuts": [dict(c) for c in targets.cuts],
        **(
            {"targets_decisions": targets.decisions}
            if targets.decisions is not None
            else {}
        ),
    }


def _row_rank(row: Mapping[str, Any], position: int) -> int:
    rank = row.get("rank")
    return (
        rank
        if isinstance(rank, int) and not isinstance(rank, bool) and rank >= 1
        else position
    )


async def snapshot_top_symbols(
    *,
    market: Market,
    top_n: int,
    snapshot_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    presets_fn: Callable[[str], Sequence[Any]] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Union of the top ``top_n`` rows of every available screener preset.

    DB-only: ``screen_stocks_snapshot_impl`` makes no HTTP call and — being the
    impl, not the registered MCP tool — never writes ``screener_pick_log``
    (hard rule 13: the observer is wired in the registration layer only). No
    observer sink argument is passed. Symbols are ordered by best rank across
    presets, then by preset order.
    """
    if snapshot_fn is None:
        from app.mcp_server.tooling.screener_snapshot_tool import (
            screen_stocks_snapshot_impl as snapshot_fn,
        )
    if presets_fn is None:
        from app.services.invest_view_model.screener_presets import (
            preset_definitions as presets_fn,
        )

    best: dict[str, tuple[int, int]] = {}
    errors: list[dict[str, str]] = []
    limit = max(int(top_n), 1)
    available = [
        p for p in presets_fn(market) if getattr(p, "availability", None) == "active"
    ]
    for preset_index, preset in enumerate(available):
        preset_id = str(getattr(preset, "id", ""))
        try:
            payload = await snapshot_fn(preset=preset_id, market=market, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one preset must not stop target selection
            errors.append(
                {
                    "origin": "screener_snapshot",
                    "preset": preset_id,
                    "error_class": type(exc).__name__,
                }
            )
            continue
        if isinstance(payload, Mapping) and payload.get("error"):
            errors.append(
                {
                    "origin": "screener_snapshot",
                    "preset": preset_id,
                    "error_class": "SnapshotError",
                }
            )
            continue
        rows = (payload or {}).get("results") or []
        for position, row in enumerate(rows[:limit], start=1):
            symbol = normalize_symbol(market, row.get("symbol"))
            if symbol is None:
                continue
            key = (_row_rank(row, position), preset_index)
            if symbol not in best or key < best[symbol]:
                best[symbol] = key
    ordered = [
        symbol for symbol, _ in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    return ordered, errors


async def build_targets(
    *,
    market: Market,
    session: AsyncSession,
    kis: KisMinuteReader,
    user_id: int,
    fixed_symbols_raw: str | None,
    now: datetime,
    screener_top_n: int = DEFAULT_SCREENER_TOP_N,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    snapshot_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    presets_fn: Callable[[str], Sequence[Any]] | None = None,
    fixed_budget: int = DEFAULT_FIXED_BUDGET,
    intraday_budget: int = DEFAULT_INTRADAY_BUDGET,
    screener_budget: int = DEFAULT_SCREENER_BUDGET,
    us_mover_dollar_volume_floor: Decimal = DEFAULT_US_MOVER_DOLLAR_VOLUME_FLOOR,
) -> TargetSet:
    """Tiered selection under ``max_symbols`` (hk 6299).

    1. ``fixed``: KIS + manual holdings and the fixed list. Never cut; its unused
       part of ``fixed_budget`` spills to the next tier.
    2. ``intraday_movers``: today's first captures — hk 6154 for KR, ordered by
       earliest capture then best rank. For US (hk 6510 rows, ET trading day, hk
       6554) every run of today is scored by ``us_mover_score.score_us_movers``:
       assets ``unknown`` last, a dollar-volume floor bucket, best rank, then
       dollar volume; warrants excluded. ``intraday_budget``. A symbol that already has minute rows
       today is protected from the budget (never demoted the same day).
    3. ``screener_snapshot``: top ``screener_top_n`` of every active preset, by best
       rank; ``screener_budget``.
    4. ``screener_pick_log``: today's admitted symbols, the remainder of the cap.

    Symbols a tier does not take remain eligible for the lower tiers; the ones
    nobody takes are reported in ``cuts`` with their tier and reason. ``truncated``
    keeps the cut count per tier.
    """
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    found: dict[str, list[str]] = {}
    errors: list[dict[str, str]] = []

    def add(origin: str, values: Iterable[object]) -> None:
        symbols = (normalize_symbol(market, value) for value in values)
        found[origin] = sorted({s for s in symbols if s})

    try:
        if market == "kr":
            holdings = await kis.fetch_my_stocks()
            add("kis_holdings", (item.get("pdno") for item in holdings))
        else:
            holdings = await kis.fetch_my_us_stocks()
            add("kis_holdings", (item.get("ovrs_pdno") for item in holdings))
    except Exception as exc:  # noqa: BLE001 - other origins still count
        errors.append({"origin": "kis_holdings", "error_class": type(exc).__name__})

    try:
        manual = await ManualHoldingsService(session).get_holdings_by_user(
            user_id=user_id,
            market_type=MarketType.KR if market == "kr" else MarketType.US,
        )
        add("manual_holdings", (getattr(h, "ticker", None) for h in manual))
    except Exception as exc:  # noqa: BLE001
        errors.append({"origin": "manual_holdings", "error_class": type(exc).__name__})

    found["fixed_list"] = parse_symbol_list(market, fixed_symbols_raw)

    try:
        ranked, snapshot_errors = await snapshot_top_symbols(
            market=market,
            top_n=screener_top_n,
            snapshot_fn=snapshot_fn,
            presets_fn=presets_fn,
        )
        found["screener_snapshot"] = ranked
        errors.extend(snapshot_errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {"origin": "screener_snapshot", "error_class": type(exc).__name__}
        )

    try:
        if market == "kr":
            start, end = kst_day_window(now)
        else:
            start, end = now - US_PICK_LOOKBACK, now + timedelta(minutes=1)
        rows = await session.execute(
            text(
                "SELECT DISTINCT symbol FROM review.screener_pick_log "
                "WHERE market = :market AND admission = 'admitted' "
                "AND recorded_at >= :start AND recorded_at < :end"
            ),
            {"market": market, "start": start, "end": end},
        )
        add("screener_pick_log", rows.scalars().all())
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        errors.append(
            {"origin": "screener_pick_log", "error_class": type(exc).__name__}
        )

    mover_order: list[str] = []
    mover_scores: dict[str, UsMoverScore] = {}
    excluded_movers: list[UsMoverScore] = []
    protected: set[str] = set()
    try:
        from app.services.invest_momentum_events.intraday import (
            first_captures,
            us_mover_rows_today,
        )

        # hk 6154 (KR) / hk 6510 (US): the observation log's trading date is the
        # KST date in KR and the ET trading day in US.
        trading_day = (
            now.astimezone(_KST).date()
            if market == "kr"
            else now.astimezone(_ET).date()
        )
        if market == "us":
            # hk 6554: best rank over every run of today, then liquidity (a floor
            # bucket); rights and warrants are excluded with their reason.
            scored, excluded_movers = score_us_movers(
                await us_mover_rows_today(session, trading_day),
                dollar_volume_floor=us_mover_dollar_volume_floor,
            )
            mover_scores = {score.symbol: score for score in scored}
            mover_order = [score.symbol for score in scored]
        else:
            captures = await first_captures(session, trading_day, market=market)
            # Promotion order: earliest capture first, then best rank, then symbol.
            ranked = sorted(
                {c.symbol: c for c in captures}.values(),
                key=lambda c: (c.snapshot_at, c.rank, c.symbol),
            )
            for capture in ranked:
                symbol = normalize_symbol(market, capture.symbol)
                if symbol and symbol not in mover_order:
                    mover_order.append(symbol)
        if mover_order:
            found["intraday_movers"] = list(mover_order)
    except Exception as exc:  # noqa: BLE001 - other origins still count
        await session.rollback()
        errors.append({"origin": "intraday_movers", "error_class": type(exc).__name__})
    have_today: set[str] = set()
    earliest_today: dict[str, datetime] = {}
    have_known = False
    if mover_order:
        try:
            if market == "us":
                # hk 6510: presence is not completion — a failed earlier page
                # leaves the newest page stored, so the earliest bar of today
                # decides whether the backfill may stop for this symbol.
                earliest_today = await load_earliest_rows_today(
                    session, mover_order, now, market
                )
                have_today = set(earliest_today)
            else:
                have_today = await load_symbols_with_rows_today(
                    session, mover_order, now, market
                )
            have_known = True
        except Exception as exc:  # noqa: BLE001 - no protection, the run continues
            await session.rollback()
            errors.append(
                {"origin": "intraday_movers", "error_class": type(exc).__name__}
            )
        protected = {s for s in mover_order if s in have_today}
    mover_candidates = [s for s in mover_order if s in protected] + [
        s for s in mover_order if s not in protected
    ]

    fixed_tier = [
        *found.get("kis_holdings", []),
        *found.get("manual_holdings", []),
        *found.get("fixed_list", []),
    ]
    specs = [
        TierSpec(TIER_FIXED, fixed_tier, budget=fixed_budget, exempt=True),
        TierSpec(
            "intraday_movers",
            mover_candidates,
            budget=intraday_budget,
            protected=frozenset(protected),
        ),
    ]
    specs.append(
        TierSpec(
            "screener_snapshot",
            found.get("screener_snapshot", []),
            budget=screener_budget,
        )
    )
    specs.append(
        TierSpec("screener_pick_log", found.get("screener_pick_log", []), budget=None)
    )
    selection = select_tiered(specs, max_symbols)

    cuts = selection.cuts
    decisions: dict[str, Any] | None = None
    if market == "us":
        cuts, decisions = _us_decisions(
            selection,
            mover_order=mover_order,
            mover_scores=mover_scores,
            excluded=excluded_movers,
            protected=protected,
            trading_day=now.astimezone(_ET).date(),
            floor=us_mover_dollar_volume_floor,
        )
    kept = set(selection.selected)
    backfill: list[str] = []
    if have_known:
        if market == "us":
            # hk 6510: retry until the earlier pages reached 04:00 ET (a symbol
            # with no rows today has not been paged at all yet).
            session_start = us_session_start_utc(now)
            backfill = sorted(
                s
                for s in mover_order
                if s in kept
                and earliest_today.get(s, session_start + timedelta(minutes=1))
                > session_start
            )
        else:
            # Kept movers with no minute row yet today get the earlier KIS page
            # (hk 6154).
            backfill = sorted(
                s for s in mover_order if s in kept and s not in have_today
            )
    return TargetSet(
        symbols=sorted(selection.selected),
        by_origin={origin: len(values) for origin, values in found.items() if values},
        errors=errors,
        truncated=selection.truncated,
        backfill=backfill,
        tiers=selection.tiers,
        cuts=cuts,
        decisions=decisions,
    )


def _us_decisions(
    selection: TieredSelection,
    *,
    mover_order: Sequence[str],
    mover_scores: Mapping[str, UsMoverScore],
    excluded: Sequence[UsMoverScore],
    protected: set[str],
    trading_day: date,
    floor: Decimal,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Final per-symbol decision of one US run (hk 6554).

    The record is the final outcome: a symbol the movers tier excluded still shows
    as ``selected`` when a lower tier took it. A symbol excluded from the movers
    tier and not taken elsewhere is cut at ``intraday_movers`` with
    ``not_common_or_etf`` (its exclusion comes before any budget). Returns the
    cut list (exclusions replace the ``select_tiered`` entry for those symbols)
    and the record for the run JSON.
    """
    excluded_by = {score.symbol: score for score in excluded}
    position = {symbol: index for index, symbol in enumerate(mover_order, start=1)}
    cut_by = {item["symbol"]: item for item in selection.cuts}
    records: list[dict[str, Any]] = []
    for symbol in sorted(set(selection.taken_by) | set(cut_by) | set(excluded_by)):
        score = mover_scores.get(symbol) or excluded_by.get(symbol)
        score_report = (
            None
            if score is None
            else score.as_report(
                protected=symbol in protected, position=position.get(symbol)
            )
        )
        if symbol in selection.taken_by:
            tier, decision, reason = selection.taken_by[symbol], "selected", None
        elif symbol in excluded_by:
            tier, decision, reason = MOVERS_TIER, "cut", REASON_NOT_COMMON_OR_ETF
        else:
            tier, decision = cut_by[symbol]["tier"], "cut"
            reason = cut_by[symbol]["reason"]
        records.append(
            {
                "symbol": symbol,
                "tier": tier,
                "decision": decision,
                "reason": reason,
                "score": score_report,
            }
        )
    cuts = [item for item in selection.cuts if item["symbol"] not in excluded_by]
    cuts.extend(
        {"symbol": symbol, "tier": MOVERS_TIER, "reason": REASON_NOT_COMMON_OR_ETF}
        for symbol in sorted(excluded_by)
        if symbol not in selection.taken_by
    )
    return cuts, {
        "trading_day": trading_day.isoformat(),
        "dollar_volume_floor": plain_decimal(floor),
        "decisions": records,
    }


async def load_symbols_with_rows_today(
    session: AsyncSession,
    symbols: Sequence[str],
    now: datetime,
    market: Market = "kr",
) -> set[str]:
    """Symbols that already have at least one minute row for today.

    KR: ``research.kr_candles_1m.session_date_kst`` on today's KST date.
    US (hk 6510): ``research.us_candles_1m.session_date`` on today's ET trading day.
    """
    if not symbols:
        return set()
    if market == "kr":
        statement = (
            f"SELECT DISTINCT symbol FROM {KR_TABLE} "
            "WHERE symbol = ANY(:symbols) AND session_date_kst = :day"
        )
        day = now.astimezone(_KST).date()
    else:
        statement = (
            f"SELECT DISTINCT symbol FROM {US_TABLE} "
            "WHERE symbol = ANY(:symbols) AND session_date = :day"
        )
        day = now.astimezone(_ET).date()
    rows = await session.execute(
        text(statement), {"symbols": list(symbols), "day": day}
    )
    return {str(value) for value in rows.scalars().all()}


def us_session_start_utc(now: datetime) -> datetime:
    """04:00 ET of ``now``'s ET trading day, as an aware UTC instant."""
    et_day = now.astimezone(_ET).date()
    return datetime.combine(et_day, US_SESSION_START_ET, tzinfo=_ET).astimezone(UTC)


async def load_earliest_rows_today(
    session: AsyncSession, symbols: Sequence[str], now: datetime, market: Market = "us"
) -> dict[str, datetime]:
    """Earliest minute row per symbol for today's session (hk 6510).

    US: earliest **KIS** bar of today's ET trading day. The earliest bar tells
    whether the backfill of a newly promoted mover already reached the 04:00 ET
    session start; row presence alone does not, because a failed earlier page
    still leaves the newest page stored.

    Only KIS rows count (``source = 'KIS'`` and a KIS venue): the Toss pass writes
    TOSS_INTEGRATED day-market bars under the same ``session_date`` with times
    before the session start (the ET day rolls at 20:00), and counting those would
    mark a mover backfilled although no KIS page ever reached 04:00 ET.
    """
    if not symbols:
        return {}
    if market == "kr":
        statement = (
            f"SELECT symbol, MIN(time_utc) FROM {KR_TABLE} "
            "WHERE symbol = ANY(:symbols) AND session_date_kst = :day "
            "GROUP BY symbol"
        )
        day = now.astimezone(_KST).date()
    else:
        statement = (
            f"SELECT symbol, MIN(time_utc) FROM {US_TABLE} "
            "WHERE symbol = ANY(:symbols) AND session_date = :day "
            f"AND source = '{SOURCE}' AND venue IN ('NASD', 'NYSE', 'AMEX') "
            "GROUP BY symbol"
        )
        day = now.astimezone(_ET).date()
    rows = await session.execute(
        text(statement), {"symbols": list(symbols), "day": day}
    )
    return {str(symbol): earliest for symbol, earliest in rows.all()}


async def load_nxt_eligible(session: AsyncSession, symbols: Sequence[str]) -> set[str]:
    if not symbols:
        return set()
    rows = await session.execute(
        text(
            "SELECT symbol FROM public.kr_symbol_universe "
            "WHERE symbol = ANY(:symbols) AND nxt_eligible IS TRUE"
        ),
        {"symbols": list(symbols)},
    )
    return {str(value) for value in rows.scalars().all()}


async def resolve_us_exchanges(
    session: AsyncSession, symbols: Sequence[str]
) -> tuple[dict[str, str], list[str]]:
    from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for symbol in symbols:
        try:
            exchange = str(await get_us_exchange_by_symbol(symbol, db=session))
        except Exception:  # noqa: BLE001 - reported as an error row, run continues
            await session.rollback()
            unresolved.append(symbol)
            continue
        if exchange in _US_VENUES:
            resolved[symbol] = exchange
        else:
            unresolved.append(symbol)
    return resolved, unresolved


# ------------------------------------------------------------------------ run


def new_batch_id(market: Market, now: datetime) -> str:
    return f"{BATCH_PREFIX}-{market}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


async def _with_earlier_page(
    kis: KisMinuteReader, frame: pd.DataFrame, *, symbol: str, venue: str
) -> tuple[pd.DataFrame, int]:
    """Append ONE earlier KIS page to ``frame`` for a newly seen mover (hk 6154).

    KIS returns about 30 bars per call whatever ``n`` is, so a symbol first seen
    mid-session would only get the last ~30 minutes. The earlier page is read
    with the hour cursor (``FID_INPUT_HOUR_1`` = the earliest bar's time in the
    first page); an overlapping bar is harmless (same key, ``DO NOTHING``).
    Nothing is requested when the first page is empty or already starts at the
    venue's first minute. A failing earlier read raises; the caller keeps the
    first page and reports the error, so the newest page is never lost.

    Returns ``(combined_frame, pages_read)``.
    """
    if frame is None or frame.empty:
        return frame, 0
    stamps = pd.to_datetime(frame["datetime"], errors="coerce").dropna()
    if stamps.empty:
        return frame, 0
    earliest = stamps.min()
    first_minute = _KR_DAY_FIRST_MINUTE.get(venue)
    if first_minute is not None and earliest.time() <= first_minute:
        return frame, 0
    older = await kis.inquire_minute_chart(
        symbol,
        market=KR_VENUE_MARKET_CODE[venue],
        time_unit=1,
        n=200,
        end_time=earliest.strftime("%H%M%S"),
    )
    if older is None or older.empty:
        return frame, 1
    combined = pd.concat([older, frame], ignore_index=True)
    combined = combined.drop_duplicates(subset=["datetime"], keep="last")
    return combined.sort_values("datetime").reset_index(drop=True), 1


async def _with_earlier_us_pages(
    kis: KisMinuteReader,
    page: Any,
    *,
    symbol: str,
    venue: str,
    session_start_et: datetime,
    max_pages: int = US_BACKFILL_MAX_PAGES,
) -> tuple[pd.DataFrame, int]:
    """Append earlier KIS overseas pages back to the session start (hk 6510).

    A US symbol first seen mid-session would otherwise only get the newest page
    (~120 minutes). The ``keyb`` cursor pages backwards until the oldest bar has
    reached ``session_start_et`` (04:00 ET of the trading day), the provider says
    there is no more history, or ``max_pages`` is spent. Overlapping bars are
    harmless (same key, ``DO NOTHING``). A failing page raises; the caller keeps
    the newest page and reports the error, so the newest page is never lost.

    Returns ``(combined_frame, pages_read)``.
    """
    frame = page.frame
    keyb = getattr(page, "next_keyb", None)
    has_more = bool(getattr(page, "has_more", False))
    pages = 0
    while has_more and keyb and pages < max_pages:
        older = await kis.inquire_overseas_minute_chart(
            symbol, exchange_code=venue, n=120, keyb=keyb
        )
        pages += 1
        older_frame = getattr(older, "frame", None)
        if older_frame is None or older_frame.empty:
            break
        frame = pd.concat([older_frame, frame], ignore_index=True)
        stamps = pd.to_datetime(older_frame["datetime"], errors="coerce").dropna()
        if stamps.empty or stamps.min() <= session_start_et:
            break
        has_more = bool(getattr(older, "has_more", False))
        keyb = getattr(older, "next_keyb", None)
    if pages:
        frame = (
            frame.drop_duplicates(subset=["datetime"], keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
    return frame, pages


async def collect(
    *,
    market: Market,
    symbols: Sequence[str],
    kis: KisMinuteReader,
    writer: MinuteWriter | None,
    nxt_eligible: Iterable[str] = (),
    us_exchanges: Mapping[str, str] | None = None,
    unresolved_symbols: Sequence[str] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    batch_id: str | None = None,
    dry_run: bool = False,
    prior_errors: Sequence[Mapping[str, str]] = (),
    backfill_symbols: Iterable[str] = (),
    toss: TossMinuteReader | None = None,
    us_calendar: TossMarketCalendar | None = None,
    toss_lookback: timedelta = TOSS_US_LOOKBACK,
) -> dict[str, Any]:
    """Read, filter to closed bars, write. ``writer`` is unused when ``dry_run``.

    US only (hk 6322): ``us_calendar`` is the Toss US market calendar of this run
    and ``toss`` the Toss reader. Without ``toss`` no Toss read happens. Without a
    calendar the Toss pass records ``CalendarUnavailable`` and reads nothing, and
    KIS labels are unchanged (no day-market window is known).
    """
    batch = batch_id or new_batch_id(market, clock())
    nxt = set(nxt_eligible)
    backfill = set(backfill_symbols)
    backfill_pages = 0
    # KIS frames are labelled in naive ET, so the paging floor is naive ET too.
    us_session_start = (
        datetime.combine(clock().astimezone(_ET).date(), US_SESSION_START_ET)
        if market == "us"
        else None
    )
    exchanges = us_exchanges or {}
    inserted = already_present = skipped_forming = skipped_invalid = 0
    null_value = would_write = 0
    reads = 0
    kis_day_market_skips = 0
    toss_reads = 0
    toss_not_listed_symbols: list[str] = []
    toss_status = "not_requested"
    errors: list[dict[str, str]] = [dict(item) for item in prior_errors]
    for symbol in unresolved_symbols:
        errors.append({"symbol": symbol, "error_class": "UnresolvedSymbol"})

    plan: list[tuple[str, str]] = []  # (symbol, venue)
    for symbol in symbols:
        if market == "kr":
            plan.append((symbol, "KRX"))
            if symbol in nxt:
                plan.append((symbol, "NTX"))
        elif symbol in exchanges:
            plan.append((symbol, exchanges[symbol]))

    for symbol, venue in plan:
        observed_before = clock()
        if (
            market == "us"
            and us_calendar is not None
            and us_day_market_bar(observed_before, us_calendar)
            and us_day_market_bar(observed_before - KIS_DAY_MARKET_GRACE, us_calendar)
        ):
            # Past the grace period KIS has no US bars in the day-market window; Toss
            # covers it (hk 6322). Within the grace KIS is still read (tail bars).
            kis_day_market_skips += 1
            continue
        try:
            if market == "kr":
                frame = await kis.inquire_minute_chart(
                    symbol, market=KR_VENUE_MARKET_CODE[venue], time_unit=1, n=200
                )
                if symbol in backfill:
                    try:
                        frame, extra = await _with_earlier_page(
                            kis, frame, symbol=symbol, venue=venue
                        )
                        backfill_pages += extra
                        reads += extra
                    except Exception as exc:  # noqa: BLE001 - keep the first page
                        errors.append(
                            {
                                "symbol": symbol,
                                "venue": venue,
                                "origin": "intraday_movers",
                                "error_class": type(exc).__name__,
                            }
                        )
            else:
                page = await kis.inquire_overseas_minute_chart(
                    symbol, exchange_code=venue, n=120
                )
                frame = page.frame
                if symbol in backfill and us_session_start is not None:
                    try:
                        frame, extra = await _with_earlier_us_pages(
                            kis,
                            page,
                            symbol=symbol,
                            venue=venue,
                            session_start_et=us_session_start,
                        )
                        backfill_pages += extra
                        reads += extra
                    except Exception as exc:  # noqa: BLE001 - keep the first page
                        errors.append(
                            {
                                "symbol": symbol,
                                "venue": venue,
                                "origin": "intraday_movers",
                                "error_class": type(exc).__name__,
                            }
                        )
            reads += 1
            parsed = bars_from_frame(
                frame,
                market=market,
                symbol=symbol,
                venue=venue,
                observed_before=observed_before,
            )
            skipped_forming += parsed.skipped_forming
            skipped_invalid += parsed.skipped_invalid
            null_value += parsed.null_value
            if dry_run:
                would_write += len(parsed.bars)
            elif parsed.bars:
                assert writer is not None
                auction = (
                    {"auction_observed_before": observed_before}
                    if venue == "KRX"
                    else {}
                )
                new, present = await writer.insert(
                    parsed.bars, batch_id=batch, retrieved_at=clock(), **auction
                )
                inserted += new
                already_present += present
        except Exception as exc:  # noqa: BLE001 - one symbol x venue must not stop the run
            logger.warning(
                "session minutes read/write failed symbol=%s venue=%s error=%s",
                symbol,
                venue,
                type(exc).__name__,
            )
            errors.append(
                {"symbol": symbol, "venue": venue, "error_class": type(exc).__name__}
            )

    if market == "us" and toss is not None:
        if us_calendar is None:
            toss_status = "calendar_unavailable"
            errors.append(
                {"venue": TOSS_VENUE_ERROR, "error_class": "CalendarUnavailable"}
            )
        else:
            now = clock()
            if calendar_overlaps(us_calendar, now - toss_lookback, now):
                toss_status = "ran"
                for symbol in symbols:
                    observed_before = clock()
                    try:
                        candles = await toss.collect_minute_candles(
                            symbol,
                            adjusted=False,
                            before=(observed_before + TOSS_CURSOR_SLACK)
                            .replace(microsecond=0)
                            .isoformat(),
                            not_before=observed_before - toss_lookback,
                            max_pages=TOSS_US_MAX_PAGES,
                        )
                        toss_reads += 1
                        parsed = bars_from_toss_candles(
                            candles,
                            symbol=symbol,
                            observed_before=observed_before,
                            us_calendar=us_calendar,
                        )
                        skipped_forming += parsed.skipped_forming
                        skipped_invalid += parsed.skipped_invalid
                        if dry_run:
                            would_write += len(parsed.bars)
                        elif parsed.bars:
                            assert writer is not None
                            new, present = await writer.insert(
                                parsed.bars, batch_id=batch, retrieved_at=clock()
                            )
                            inserted += new
                            already_present += present
                    except Exception as exc:  # noqa: BLE001 - one symbol must not stop the run
                        from app.services.brokers.toss.errors import (
                            TossApiResponseError,
                        )

                        if (
                            isinstance(exc, TossApiResponseError)
                            and exc.status_code == 404
                            and exc.envelope.code == "stock-not-found"
                        ):
                            toss_not_listed_symbols.append(symbol)
                            logger.info(
                                "session minutes toss symbol not listed symbol=%s",
                                symbol,
                            )
                            continue
                        logger.warning(
                            "session minutes toss read/write failed symbol=%s error=%s",
                            symbol,
                            type(exc).__name__,
                        )
                        errors.append(
                            {
                                "symbol": symbol,
                                "venue": TOSS_VENUE_ERROR,
                                "error_class": type(exc).__name__,
                            }
                        )
            else:
                toss_status = "no_window"

    return {
        "market": market,
        "batch_id": batch,
        "dry_run": dry_run,
        "symbols": len(symbols),
        "reads": reads,
        "backfill_symbols": len(backfill),
        "backfill_pages": backfill_pages,
        "inserted": inserted,
        "already_present": already_present,
        "skipped_forming": skipped_forming,
        "skipped_invalid": skipped_invalid,
        "null_value": null_value,
        "would_write": would_write if dry_run else None,
        "errors": len(errors),
        "error_details": errors,
        "kis_skipped_day_market": kis_day_market_skips,
        "toss": toss_status,
        "toss_reads": toss_reads,
        "toss_not_listed": {
            "count": len(toss_not_listed_symbols),
            "symbols": toss_not_listed_symbols,
        },
    }


__all__ = [
    "CUT_REASON_BUDGET",
    "KisMinuteReader",
    "MinuteBar",
    "MinuteWriter",
    "SqlMinuteWriter",
    "TossMinuteReader",
    "bars_from_toss_candles",
    "calendar_overlaps",
    "us_calendar_segment",
    "TIER_ORDER",
    "TargetSet",
    "TierReport",
    "TierSpec",
    "TieredSelection",
    "select_tiered",
    "select_with_cap",
    "targets_report",
    "snapshot_top_symbols",
    "bars_from_frame",
    "build_targets",
    "collect",
    "is_closed",
    "load_nxt_eligible",
    "load_symbols_with_rows_today",
    "new_batch_id",
    "normalize_symbol",
    "parse_symbol_list",
    "resolve_us_exchanges",
]
