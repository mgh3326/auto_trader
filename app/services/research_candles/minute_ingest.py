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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.research_candles.session_minutes_collector import (
    KisMinuteReader,
    MinuteBar,
    TargetSet,
    build_targets,
    normalize_symbol,
    targets_report,
)
from app.services.research_candles.session_segment import (
    session_segment_of,
    us_session_date,
)

Market = Literal["kr", "us"]

_KR = ZoneInfo("Asia/Seoul")

#: A receipt claim this much ahead of the server clock is refused as future
#: (transport clock skew only). A late claim is only bounded by the bar start.
MAX_CLOCK_SKEW = timedelta(seconds=5)

#: Closed ``source`` vocabulary per market — exactly what the storage CHECKs of
#: ``research.kr_candles_1m`` / ``research.us_candles_1m`` admit. Extending this
#: to a new source (KIWOOM US, NH_MOCK, UPBIT, …) needs its own additive
#: migration, so this surface never accepts a value the table would reject.
SOURCES_BY_MARKET: dict[str, frozenset[str]] = {
    "kr": frozenset({"KIWOOM", "KIS", "TOSS", "UNKNOWN"}),
    "us": frozenset({"KIS", "TOSS", "YAHOO", "UNKNOWN"}),
}

#: Closed ``venue`` vocabulary per market (table CHECKs).
VENUES_BY_MARKET: dict[str, frozenset[str]] = {
    "kr": frozenset({"KRX", "NTX"}),
    "us": frozenset({"NASD", "NYSE", "AMEX"}),
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
REASON_RECEIVED_AT_INVALID = "received_at_invalid"
REASON_RECEIVED_AT_IN_FUTURE = "received_at_in_future"
REASON_RECEIVED_AT_BEFORE_BAR = "received_at_before_bar"
REASON_INVALID_PRICE = "invalid_price"
REASON_INCONSISTENT_OHLC = "inconsistent_ohlc"
REASON_INVALID_VOLUME = "invalid_volume"
REASON_INVALID_VALUE = "invalid_value"
REASON_VALUE_REQUIRED = "value_required"

REASONS: frozenset[str] = frozenset(
    {
        REASON_UNKNOWN_SOURCE,
        REASON_UNKNOWN_VENUE,
        REASON_INVALID_SYMBOL,
        REASON_BAR_TIME_INVALID,
        REASON_BAR_TIME_NOT_MINUTE_ALIGNED,
        REASON_RECEIVED_AT_INVALID,
        REASON_RECEIVED_AT_IN_FUTURE,
        REASON_RECEIVED_AT_BEFORE_BAR,
        REASON_INVALID_PRICE,
        REASON_INCONSISTENT_OHLC,
        REASON_INVALID_VOLUME,
        REASON_INVALID_VALUE,
        REASON_VALUE_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class BarDecision:
    """One item's outcome: the row to write, or the reason it is refused."""

    bar: MinuteBar | None = None
    retrieved_at: datetime | None = None
    reason: str | None = None


def _valid_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    offset = value.utcoffset()
    if offset is None:
        return None
    return value.astimezone(UTC)


def _finite(value: object) -> Decimal | None:
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    return value


def build_batch_id(market: Market, now: datetime) -> str:
    return f"{BATCH_PREFIX}-{market}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def decide_bar(
    *,
    market: Market,
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
) -> BarDecision:
    """Validate one posted bar and derive the server-owned fields.

    ``now`` is the server clock read once per request; a claim beyond it (plus
    ``MAX_CLOCK_SKEW``) is refused rather than stored.
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

    start = _valid_datetime(time_utc)
    if start is None:
        return BarDecision(reason=REASON_BAR_TIME_INVALID)
    if start.second or start.microsecond:
        return BarDecision(reason=REASON_BAR_TIME_NOT_MINUTE_ALIGNED)

    received = _valid_datetime(received_at)
    if received is None:
        return BarDecision(reason=REASON_RECEIVED_AT_INVALID)
    if received > now + MAX_CLOCK_SKEW:
        return BarDecision(reason=REASON_RECEIVED_AT_IN_FUTURE)
    if received < start:
        return BarDecision(reason=REASON_RECEIVED_AT_BEFORE_BAR)

    o = _finite(open_price)
    h = _finite(high_price)
    lo = _finite(low_price)
    c = _finite(close_price)
    if o is None or h is None or lo is None or c is None:
        return BarDecision(reason=REASON_INVALID_PRICE)
    if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
        return BarDecision(reason=REASON_INVALID_PRICE)
    if h < max(o, c, lo) or lo > min(o, c):
        return BarDecision(reason=REASON_INCONSISTENT_OHLC)

    traded = _finite(volume)
    if traded is None or traded < 0:
        return BarDecision(reason=REASON_INVALID_VOLUME)

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

    session_date = (
        start.astimezone(_KR).date() if market == "kr" else us_session_date(start)
    )
    return BarDecision(
        bar=MinuteBar(
            time_utc=start,
            session_date=session_date,
            symbol=normalized,
            venue=venue_code,
            session_segment=session_segment_of(market, venue_code, start),
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
    "REASONS",
    "SOURCES_BY_MARKET",
    "VENUES_BY_MARKET",
    "build_batch_id",
    "build_targets_payload",
    "decide_bar",
]
