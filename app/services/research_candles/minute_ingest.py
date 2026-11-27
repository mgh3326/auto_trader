"""hk 6343: closed validation for the research minute-bar HTTP ingest.

The Go quote collector posts closed 1-minute bars over the token-authed
``/trading/api/research-minutes/ingest`` surface. This module is the pure part
of that surface: it decides, per bar, whether the row may be written, and it
derives every field the server owns instead of trusting the producer.

Server-owned fields (a producer cannot set them):

* ``session_segment`` — ``session_segment_of`` (``session_segment.py``), the
  same rule the Python collector uses. Never taken from the wire.
* ``session_date``/``session_date_kst`` — derived from ``time_utc`` (KST date
  for KR, the 20:00 ET rollover for US).
* ``retrieved_at`` — the client's ``received_at`` after validation. It is the
  first receipt, never a later "now": rows are insert-only, so a replay leaves
  it unchanged.

``received_at`` rules (design hk:doc design/6321-go-quote-collector.md §1.2):

* must be timezone-aware;
* must not be later than the server clock plus ``MAX_CLOCK_SKEW`` — a producer
  cannot claim a future receipt;
* must not be earlier than the bar's own start (a bar cannot be received before
  it began), so a retry or backfill cannot claim an earlier receipt than its
  bar.

Code and closed reason vocabulary only: no order, ledger, broker-mutation or
approval module is imported here.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import parse_futures_contract_symbol
from app.services.kis_futures_quotes.contracts import (
    CLOSED_QUOTE_PRODUCTS,
)
from app.services.kis_futures_quotes.contracts import (
    roll_state as roll_state_of,
)
from app.services.research_candles.futures_minutes_collector import FuturesBar
from app.services.research_candles.session_minutes_collector import (
    KisMinuteReader,
    MinuteBar,
    TargetSet,
    build_targets,
    is_closed,
    normalize_symbol,
    targets_report,
)
from app.services.research_candles.session_segment import (
    KIS_DAY_VENUE,
    futures_session_date,
    session_segment_of,
    us_session_date,
)

#: Stock markets (``research.kr_candles_1m`` / ``research.us_candles_1m``).
StockMarket = Literal["kr", "us"]
#: Every market the ingest surface accepts. ``futures`` lands in
#: ``research.kr_futures_candles_1m`` (hk 6148 contract, hk 6424 POST).
Market = Literal["kr", "us", "futures"]

_KR = ZoneInfo("Asia/Seoul")

#: Closed futures product codes (KOSPI200 futures, mini KOSPI200 futures).
_FUTURES_PRODUCT_CODES = frozenset(product.code for product in CLOSED_QUOTE_PRODUCTS)

#: A receipt claim this much ahead of the server clock is refused as future
#: (transport clock skew only). A late claim is only bounded by the bar start.
MAX_CLOCK_SKEW = timedelta(seconds=5)

#: No research minute bar can legitimately precede this. A zero/sentinel
#: timestamp (Go's ``0001-01-01``) would otherwise be written as a permanent
#: year-1 first bar that suppresses later missing-data diagnostics.
MIN_BAR_START = datetime(2000, 1, 1, tzinfo=UTC)

#: Closed ``source`` vocabulary per market — exactly what the storage CHECKs of
#: ``research.kr_candles_1m`` / ``research.us_candles_1m`` admit. Extending this
#: to a new source (KIWOOM US, NH_MOCK, UPBIT, …) needs its own additive
#: migration, so this surface never accepts a value the table would reject.
SOURCES_BY_MARKET: dict[str, frozenset[str]] = {
    "kr": frozenset({"KIWOOM", "KIS", "TOSS", "UNKNOWN"}),
    "us": frozenset({"KIS", "TOSS", "YAHOO", "UNKNOWN"}),
    # research.kr_futures_candles_1m CHECK (hk 6148 migration).
    "futures": frozenset({"KIS", "UNKNOWN"}),
}

#: Closed ``venue`` vocabulary per market (table CHECKs). Futures has no venue
#: column: its one KRX derivatives venue is the only value it accepts.
#: US: ``KIS_DAY`` (hk 7037) is the KIS 주간거래 venue. The raw KIS day-market
#: exchange codes ``BAY``/``BAQ``/``BAA`` are not venues and are refused with
#: ``unknown_venue``; the producer must send ``KIS_DAY``.
VENUES_BY_MARKET: dict[str, frozenset[str]] = {
    "kr": frozenset({"KRX", "NTX"}),
    "us": frozenset({"NASD", "NYSE", "AMEX", KIS_DAY_VENUE}),
    "futures": frozenset({"KRX"}),
}

#: A producer-supplied ``batch_id`` (diagnostic provenance): bounded, ASCII,
#: no spaces. The collector's ``backfill`` marker survives as long as it fits.
BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,100}")
BATCH_PREFIX = "minutes-ingest"

# Closed per-item rejection reasons.
REASON_UNKNOWN_SOURCE = "unknown_source"
REASON_UNKNOWN_VENUE = "unknown_venue"
REASON_INVALID_SYMBOL = "invalid_symbol"
REASON_BAR_TIME_INVALID = "bar_time_invalid"
REASON_BAR_TIME_NOT_MINUTE_ALIGNED = "bar_time_not_minute_aligned"
REASON_BAR_NOT_CLOSED = "bar_not_closed"
REASON_RECEIVED_AT_INVALID = "received_at_invalid"
REASON_RECEIVED_AT_IN_FUTURE = "received_at_in_future"
REASON_RECEIVED_AT_BEFORE_BAR = "received_at_before_bar"
REASON_INVALID_PRICE = "invalid_price"
REASON_INCONSISTENT_OHLC = "inconsistent_ohlc"
REASON_INVALID_VOLUME = "invalid_volume"
REASON_INVALID_VALUE = "invalid_value"
REASON_VALUE_REQUIRED = "value_required"
#: A KIS_DAY bar needs the Toss US calendar to be labelled (US_DAY window). Without
#: it the label would be a guess, and insert-only rows cannot be relabelled later.
REASON_CALENDAR_UNAVAILABLE = "calendar_unavailable"
# Futures only (hk 6424). A field the market does not carry, or a value the
# server derives differently from the producer's claim.
REASON_FIELD_NOT_APPLICABLE = "field_not_applicable"
REASON_MASTER_UNAVAILABLE = "master_unavailable"
REASON_ROLL_STATE_UNVERIFIABLE = "roll_state_unverifiable"
REASON_PRODUCT_MISMATCH = "product_mismatch"
REASON_CONTRACT_MONTH_MISMATCH = "contract_month_mismatch"
REASON_ROLL_STATE_MISMATCH = "roll_state_mismatch"
REASON_SESSION_SEGMENT_MISMATCH = "session_segment_mismatch"
REASON_SESSION_DATE_MISMATCH = "session_date_mismatch"

REASONS: frozenset[str] = frozenset(
    {
        REASON_UNKNOWN_SOURCE,
        REASON_UNKNOWN_VENUE,
        REASON_INVALID_SYMBOL,
        REASON_BAR_TIME_INVALID,
        REASON_BAR_TIME_NOT_MINUTE_ALIGNED,
        REASON_BAR_NOT_CLOSED,
        REASON_RECEIVED_AT_INVALID,
        REASON_RECEIVED_AT_IN_FUTURE,
        REASON_RECEIVED_AT_BEFORE_BAR,
        REASON_INVALID_PRICE,
        REASON_INCONSISTENT_OHLC,
        REASON_INVALID_VOLUME,
        REASON_INVALID_VALUE,
        REASON_VALUE_REQUIRED,
        REASON_CALENDAR_UNAVAILABLE,
        REASON_FIELD_NOT_APPLICABLE,
        REASON_MASTER_UNAVAILABLE,
        REASON_ROLL_STATE_UNVERIFIABLE,
        REASON_PRODUCT_MISMATCH,
        REASON_CONTRACT_MONTH_MISMATCH,
        REASON_ROLL_STATE_MISMATCH,
        REASON_SESSION_SEGMENT_MISMATCH,
        REASON_SESSION_DATE_MISMATCH,
    }
)


@dataclass(frozen=True, slots=True)
class BarDecision:
    """One item's outcome: the row to write, or the reason it is refused."""

    bar: MinuteBar | FuturesBar | None = None
    retrieved_at: datetime | None = None
    reason: str | None = None


def _valid_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        # ``9999-12-31T23:59:59-01:00`` overflows when normalised to UTC.
        return None


def _finite(value: object) -> Decimal | None:
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    return value


def build_batch_id(market: Market, now: datetime) -> str:
    return f"{BATCH_PREFIX}-{market}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def _check_start(time_utc: object) -> tuple[datetime | None, str | None]:
    start = _valid_datetime(time_utc)
    if start is None:
        return None, REASON_BAR_TIME_INVALID
    if start.second or start.microsecond:
        return None, REASON_BAR_TIME_NOT_MINUTE_ALIGNED
    if start < MIN_BAR_START:
        return None, REASON_BAR_TIME_INVALID
    return start, None


def _check_receipt(
    received_at: object, start: datetime, now: datetime
) -> tuple[datetime | None, str | None]:
    received = _valid_datetime(received_at)
    if received is None:
        return None, REASON_RECEIVED_AT_INVALID
    if received > now + MAX_CLOCK_SKEW:
        return None, REASON_RECEIVED_AT_IN_FUTURE
    if received < start:
        return None, REASON_RECEIVED_AT_BEFORE_BAR
    return received, None


def _check_closed(start: datetime, received: datetime, now: datetime) -> str | None:
    # Only a closed bar may be stored: ``is_closed`` is the collector's own rule
    # (``start + 1 min + 10 s <= observed``), and ``min`` keeps a claimed
    # receipt from closing a bar earlier than the server clock allows. A
    # forming bar would be a permanent partial row (insert-only, no UPDATE).
    if not is_closed(start, min(received, now)):
        return REASON_BAR_NOT_CLOSED
    return None


def _check_ohlcv(
    open_price: object,
    high_price: object,
    low_price: object,
    close_price: object,
    volume: object,
) -> tuple[tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None, str | None]:
    o = _finite(open_price)
    h = _finite(high_price)
    lo = _finite(low_price)
    c = _finite(close_price)
    if o is None or h is None or lo is None or c is None:
        return None, REASON_INVALID_PRICE
    if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
        return None, REASON_INVALID_PRICE
    if h < max(o, c, lo) or lo > min(o, c):
        return None, REASON_INCONSISTENT_OHLC
    traded = _finite(volume)
    if traded is None or traded < 0:
        return None, REASON_INVALID_VOLUME
    return (o, h, lo, c, traded), None


@dataclass(frozen=True, slots=True)
class _FuturesIdentity:
    symbol: str  # canonical DB symbol ``<PRODUCT>.<YYMM>``
    product: str
    contract_month: str  # ``YYYY-MM`` (roll_state input)
    yymm: str  # the stored ``contract_month`` column


def _futures_identity(symbol: object) -> _FuturesIdentity | None:
    """Only the canonical symbol of a closed product is a futures contract.

    ``parse_futures_contract_symbol`` rebuilds the symbol and refuses anything
    that is not byte-identical, so a lower-case or padded symbol is not guessed
    into a contract.
    """
    if not isinstance(symbol, str):
        return None
    try:
        product, contract_month = parse_futures_contract_symbol(symbol)
    except ValueError:
        return None
    if product not in _FUTURES_PRODUCT_CODES:
        return None
    return _FuturesIdentity(
        symbol=symbol,
        product=product,
        contract_month=contract_month,
        yymm=symbol.partition(".")[2],
    )


def decide_bar(
    *,
    market: StockMarket,
    symbol: object,
    venue: object,
    time_utc: object,
    open_price: object,
    high_price: object,
    low_price: object,
    close_price: object,
    volume: object,
    value: object,
    received_at: object,
    source: object,
    now: datetime,
    us_calendar: Any | None = None,
) -> BarDecision:
    """Validate one posted kr/us bar and derive the server-owned fields.

    ``now`` is the server clock read once per request; a claim beyond it (plus
    ``MAX_CLOCK_SKEW``) is refused rather than stored.

    ``us_calendar`` is the Toss US market calendar (hk 6322) that labels a KIS_DAY
    bar ``US_DAY``. A KIS_DAY bar without it is refused (``calendar_unavailable``):
    the label would otherwise be a guess, and rows are insert-only.
    """
    label = str(source or "").strip().upper()
    if label not in SOURCES_BY_MARKET[market]:
        return BarDecision(reason=REASON_UNKNOWN_SOURCE)

    venue_code = str(venue or "").strip().upper()
    if venue_code not in VENUES_BY_MARKET[market]:
        return BarDecision(reason=REASON_UNKNOWN_VENUE)

    normalized = normalize_symbol(market, symbol)
    if normalized is None:
        return BarDecision(reason=REASON_INVALID_SYMBOL)

    start, reason = _check_start(time_utc)
    if start is None:
        return BarDecision(reason=reason)

    received, reason = _check_receipt(received_at, start, now)
    if received is None:
        return BarDecision(reason=reason)

    closed_reason = _check_closed(start, received, now)
    if closed_reason is not None:
        return BarDecision(reason=closed_reason)

    ohlcv, reason = _check_ohlcv(open_price, high_price, low_price, close_price, volume)
    if ohlcv is None:
        return BarDecision(reason=reason)
    o, h, lo, c, traded = ohlcv

    if value is None:
        if market == "us":
            # research.us_candles_1m.value is NOT NULL: the US per-bar value is
            # part of the row, so a US bar without it cannot be stored.
            return BarDecision(reason=REASON_VALUE_REQUIRED)
        traded_value = None  # KR: NULL when the per-minute value is unproven
    else:
        traded_value = _finite(value)
        if traded_value is None or traded_value < 0:
            return BarDecision(reason=REASON_INVALID_VALUE)

    if market == "us" and venue_code == KIS_DAY_VENUE and us_calendar is None:
        return BarDecision(reason=REASON_CALENDAR_UNAVAILABLE)

    try:
        session_date = (
            start.astimezone(_KR).date() if market == "kr" else us_session_date(start)
        )
    except (OverflowError, ValueError):
        return BarDecision(reason=REASON_BAR_TIME_INVALID)
    return BarDecision(
        bar=MinuteBar(
            time_utc=start,
            session_date=session_date,
            symbol=normalized,
            venue=venue_code,
            session_segment=session_segment_of(
                market, venue_code, start, us_calendar=us_calendar
            ),
            source=label,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=traded,
            value=traded_value,
        ),
        retrieved_at=received,
    )


def decide_futures_bar(
    *,
    symbol: object,
    venue: object,
    time_utc: object,
    open_price: object,
    high_price: object,
    low_price: object,
    close_price: object,
    volume: object,
    value: object,
    received_at: object,
    source: object,
    product: object,
    contract_month: object,
    roll_state: object,
    session_segment: object,
    session_date: object,
    listing: Mapping[str, tuple[str, ...]] | None,
    now: datetime,
) -> BarDecision:
    """Validate one futures bar and recompute every derived field server-side.

    ``product``, ``contract_month``, ``roll_state``, ``session_segment`` and
    ``session_date`` are optional on the wire. The server derives each one from
    the symbol and the bar start with the Python rules that the collector uses,
    and a sent value that disagrees is refused with its own closed reason: the
    server never adopts the producer's derivation.

    ``listing`` is the master's ``product -> listed YYYY-MM`` map (``None`` when
    the master could not be read). ``roll_state`` depends on which earlier months
    are still live, so without a listing the bar is refused, never stored with a
    guessed state.
    """
    label = str(source or "").strip().upper()
    if label not in SOURCES_BY_MARKET["futures"]:
        return BarDecision(reason=REASON_UNKNOWN_SOURCE)

    venue_code = str(venue or "").strip().upper()
    if venue_code not in VENUES_BY_MARKET["futures"]:
        return BarDecision(reason=REASON_UNKNOWN_VENUE)

    identity = _futures_identity(symbol)
    if identity is None:
        return BarDecision(reason=REASON_INVALID_SYMBOL)

    start, reason = _check_start(time_utc)
    if start is None:
        return BarDecision(reason=reason)

    received, reason = _check_receipt(received_at, start, now)
    if received is None:
        return BarDecision(reason=reason)

    closed_reason = _check_closed(start, received, now)
    if closed_reason is not None:
        return BarDecision(reason=closed_reason)

    ohlcv, reason = _check_ohlcv(open_price, high_price, low_price, close_price, volume)
    if ohlcv is None:
        return BarDecision(reason=reason)
    o, h, lo, c, traded = ohlcv

    if value is not None:
        # The futures table has no value column; a sent value would be dropped.
        return BarDecision(reason=REASON_FIELD_NOT_APPLICABLE)

    try:
        trading_date = futures_session_date(start)
        on_date = trading_date or start.astimezone(_KR).date()
        segment = session_segment_of("futures", venue_code, start)
    except (OverflowError, ValueError):
        return BarDecision(reason=REASON_BAR_TIME_INVALID)

    if listing is None:
        return BarDecision(reason=REASON_MASTER_UNAVAILABLE)
    listed = listing.get(identity.product)
    if listed is None:
        return BarDecision(reason=REASON_ROLL_STATE_UNVERIFIABLE)
    try:
        derived_roll = roll_state_of(identity.contract_month, on_date, list(listed))
    except ValueError:
        return BarDecision(reason=REASON_ROLL_STATE_UNVERIFIABLE)

    if product is not None and product != identity.product:
        return BarDecision(reason=REASON_PRODUCT_MISMATCH)
    if contract_month is not None and contract_month != identity.yymm:
        return BarDecision(reason=REASON_CONTRACT_MONTH_MISMATCH)
    if roll_state is not None and roll_state != derived_roll:
        return BarDecision(reason=REASON_ROLL_STATE_MISMATCH)
    if session_segment is not None and session_segment != segment:
        return BarDecision(reason=REASON_SESSION_SEGMENT_MISMATCH)
    if session_date is not None and session_date != trading_date:
        return BarDecision(reason=REASON_SESSION_DATE_MISMATCH)

    return BarDecision(
        bar=FuturesBar(
            time_utc=start,
            symbol=identity.symbol,
            product=identity.product,
            contract_month=identity.yymm,
            roll_state=derived_roll,
            session_segment=segment,
            session_date=trading_date,
            source=label,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=traded,
        ),
        retrieved_at=received,
    )


async def build_targets_payload(
    *,
    market: Market,
    session: AsyncSession,
    kis: KisMinuteReader,
    user_id: int,
    fixed_symbols_raw: str | None,
    now: datetime,
    screener_top_n: int,
    max_symbols: int,
    fixed_budget: int,
    intraday_budget: int,
    screener_budget: int,
) -> dict[str, object]:
    """The read-only ``targets`` answer: ``build_targets`` + ``targets_report``.

    The tier rule has one implementation (``build_targets``, hk 6299); this
    function only adds the run stamp and the per-origin errors the tier report
    does not carry.
    """
    targets: TargetSet = await build_targets(
        market=market,
        session=session,
        kis=kis,
        user_id=user_id,
        fixed_symbols_raw=fixed_symbols_raw,
        now=now,
        screener_top_n=screener_top_n,
        max_symbols=max_symbols,
        fixed_budget=fixed_budget,
        intraday_budget=intraday_budget,
        screener_budget=screener_budget,
    )
    payload = targets_report(targets)
    payload["market"] = market
    payload["generated_at"] = now.isoformat()
    payload["errors"] = [dict(item) for item in targets.errors]
    return payload


__all__ = [
    "BATCH_ID_PATTERN",
    "BATCH_PREFIX",
    "BarDecision",
    "MAX_CLOCK_SKEW",
    "MIN_BAR_START",
    "REASONS",
    "SOURCES_BY_MARKET",
    "VENUES_BY_MARKET",
    "build_batch_id",
    "build_targets_payload",
    "decide_bar",
    "decide_futures_bar",
]
