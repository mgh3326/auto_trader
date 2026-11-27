"""``session_observation`` — what can be seen, per market x venue x session.

hk 6421. For one session date and a symbol list it answers, session by session,
which stored feed shows the symbol, when the newest trade bar started, when the
newest bar was first received, which close it is compared against, and one
closed ``state`` with the rule (``reason``) that decided it.

Read-only: SELECT inside the reader's own snapshot (``read_snapshot``), every
source read in a SAVEPOINT (``guarded``). A failed or cut-off read is an
``unknowns`` entry and a null state, never ``not_collected``.

Sources:

* bars — ``research.kr_candles_1m`` (KST day of ``time_utc``, with the hk 6757
  auction revisions), ``research.us_candles_1m`` and
  ``research.kr_futures_candles_1m`` (stored ``session_date``). ``time_utc`` is
  the bar START and ``retrieved_at`` the first receipt; backfill and promotion
  batches are not first receipts and are not read. A volume-0 bar is a stored
  bar, never a trade.
* population — a fixed list plus the date's first-ranked symbols of
  ``invest_momentum_event_snapshots`` (hk 6154/6510), or the requested symbols;
  futures default to the contracts stored in the last days.
* reference close — the previous session's close: KRX official
  (``krx_official_daily``) when present, else the stored daily candle.

Nothing is fetched from a broker or an exchange: this reports what is stored.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.market_events.session_calendar import SessionStatus
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    MAX_SOURCE_ROWS,
    RetroReadError,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_symbol,
    dec,
    decode_cursor,
    guarded,
    iso,
    kst_window,
    latest,
    params_digest,
    parse_date,
    read_snapshot,
    safe_code,
)
from app.services.retro_v1.market import (
    MARKETS,
    infer_market,
    normalize_symbol,
    select_market,
)
from app.services.retro_v1.session_windows import (
    FUTURES_SESSIONS,
    FUTURES_SYMBOL,
    FUTURES_VENUE,
    KIS_DAY_US_VENUE,
    KR_SESSIONS,
    SESSION_ORDER,
    TOSS_US_VENUE,
    UNKNOWN_WINDOW,
    US_DAY,
    US_EXCHANGE_SESSIONS,
    Window,
    contract_expired,
    day_status,
    phase_of,
    previous_session_date,
    session_window,
)

MARKET_ORDER: Final = ("kr", "us", "futures")
#: The small fixed list every default call reports (hk 6421).
FIXED_SYMBOLS: Final[dict[str, tuple[str, ...]]] = {
    "kr": ("005930", "000660", "278470"),
    "us": ("SPY", "QQQ", "SMH"),
}
MOVER_MARKETS: Final = ("kr", "us")
DEFAULT_MOVERS: Final = 10
MAX_MOVERS: Final = 50
MAX_SYMBOLS: Final = 50
#: One threshold for both delay rules. The stored minutes come from a 5-minute
#: scheduled collector whose run may take 4 minutes, so two cadences is the
#: first age a normally running collector does not reach.
DEFAULT_MAX_LAG_SECONDS: Final = 600
MIN_MAX_LAG_SECONDS: Final = 60
MAX_MAX_LAG_SECONDS: Final = 3600
FUTURES_LOOKBACK_DAYS: Final = 7
#: How far before its session date a futures night bar can start.
FUTURES_NIGHT_REACH_DAYS: Final = 16
US_EXCHANGES: Final = ("NASD", "NYSE", "AMEX")
CURRENCY: Final = {"kr": "KRW", "us": "USD"}
_BAR: Final = timedelta(minutes=1)

# --------------------------------------------------------------------------
# SQL (SELECT only)
# --------------------------------------------------------------------------
#: A backfill or promotion batch's ``retrieved_at`` is not a first receipt (same
#: rule as ``daily_bars.is_first_receipt_batch``).
_FIRST_RECEIPT: Final = (
    "position('backfill' in lower(b.batch_id)) = 0 "
    "AND left(lower(b.batch_id), 8) <> 'promote-'"
)


def _aggregate(rows_sql: str) -> str:
    """One row per symbol x venue x segment; no bar is returned individually."""
    return f"""
    SELECT symbol, venue, session_segment,
           count(*) AS bar_count,
           count(*) FILTER (WHERE volume > 0) AS trade_bar_count,
           max(time_utc) AS last_bar_at,
           max(time_utc) FILTER (WHERE volume > 0) AS last_trade_bar_at,
           max(retrieved_at) AS last_received_at,
           (array_agg(retrieved_at ORDER BY time_utc DESC))[1]
               AS last_bar_received_at,
           (array_agg(source ORDER BY time_utc DESC))[1] AS last_bar_source,
           (array_agg(source ORDER BY time_utc DESC)
               FILTER (WHERE volume > 0))[1] AS last_trade_source,
           array_agg(DISTINCT source) AS sources
    FROM ({rows_sql}) AS bars
    GROUP BY symbol, venue, session_segment
    ORDER BY symbol, venue, session_segment
    LIMIT :cap
    """


_BARS_SQL: Final = {
    "kr": _aggregate(
        f"""
        SELECT b.symbol, b.venue, b.session_segment, b.time_utc, b.retrieved_at,
               COALESCE(r.volume, b.volume) AS volume,
               CASE WHEN r.revised_at IS NULL THEN b.source ELSE 'KIS' END
                   AS source
        FROM research.kr_candles_1m AS b
        LEFT JOIN research.kr_auction_minute_revisions AS r
            USING (time_utc, symbol, venue)
        WHERE b.symbol IN :symbols AND b.time_utc >= :lo AND b.time_utc < :hi
          AND {_FIRST_RECEIPT}
        """
    ),
    "us": _aggregate(
        f"""
        SELECT b.symbol, b.venue, b.session_segment, b.time_utc, b.retrieved_at,
               b.volume, b.source
        FROM research.us_candles_1m AS b
        WHERE b.symbol IN :symbols AND b.session_date = :day
          AND b.time_utc >= :lo AND b.time_utc < :hi
          AND {_FIRST_RECEIPT}
        """
    ),
    "futures": _aggregate(
        f"""
        SELECT b.symbol, '{FUTURES_VENUE}' AS venue, b.session_segment,
               b.time_utc, b.retrieved_at, b.volume, b.source
        FROM research.kr_futures_candles_1m AS b
        WHERE b.symbol IN :symbols AND b.session_date = :day
          AND b.time_utc >= :lo AND b.time_utc < :hi
          AND {_FIRST_RECEIPT}
        """
    ),
}
_MOVERS_SQL: Final = """
    SELECT symbol
    FROM public.invest_momentum_event_snapshots
    WHERE market = :market AND trading_date = :day AND order_type <> 'excluded'
    GROUP BY symbol
    ORDER BY min(rank), min(snapshot_at), symbol
    LIMIT :cap
"""
_CONTRACTS_SQL: Final = """
    SELECT DISTINCT symbol
    FROM research.kr_futures_candles_1m
    WHERE session_date >= :first AND session_date <= :day
    ORDER BY symbol
    LIMIT :cap
"""
_UNIVERSE_SQL: Final = {
    "kr": """
        SELECT symbol, nxt_eligible AS fact
        FROM public.kr_symbol_universe
        WHERE symbol IN :symbols
    """,
    "us": """
        SELECT symbol, exchange AS fact
        FROM public.us_symbol_universe
        WHERE symbol IN :symbols AND is_active IS TRUE
    """,
}
_KR_OFFICIAL_SQL: Final = """
    SELECT symbol, close_price AS close, source
    FROM public.krx_official_daily
    WHERE symbol IN :symbols AND session_date = :day
"""
_DAILY_SQL: Final = {
    "kr": """
        SELECT symbol, close, source
        FROM public.kr_candles_1d
        WHERE symbol IN :symbols AND venue = 'KRX'
          AND time >= :lo AND time < :hi
        ORDER BY symbol, time
    """,
    "us": """
        SELECT symbol, close, source
        FROM public.us_candles_1d
        WHERE symbol IN :symbols AND time >= :lo AND time < :hi
        ORDER BY symbol, time, exchange
    """,
}


# --------------------------------------------------------------------------
# data shapes
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionBars:
    """Aggregate of the stored first-receipt bars of one session."""

    bar_count: int
    trade_bar_count: int
    last_bar_at: datetime
    last_trade_bar_at: datetime | None
    last_received_at: datetime
    #: ``retrieved_at`` of the newest bar (``last_bar_at``).
    last_bar_received_at: datetime
    last_bar_source: str | None
    last_trade_source: str | None
    sources: tuple[str, ...]

    @property
    def receive_lag_seconds(self) -> float:
        """First receipt of the newest bar minus that bar's close."""
        return (self.last_bar_received_at - (self.last_bar_at + _BAR)).total_seconds()


BarKey = tuple[str, str, str]
"""``(symbol, venue, session)``."""


@dataclass(frozen=True, slots=True)
class Subject:
    market: str
    symbol: str
    origin: str


@dataclass(frozen=True, slots=True)
class Reference:
    price: Decimal | None
    basis: str | None
    source: str | None
    reason: str | None


# --------------------------------------------------------------------------
# pure state derivation
# --------------------------------------------------------------------------
def derive_state(
    *,
    status: SessionStatus,
    window: Window,
    bars: SessionBars | None,
    bars_read: bool,
    collection: str,
    as_of: datetime,
    max_lag_seconds: int,
) -> tuple[str | None, str]:
    """``(state, reason)`` of one symbol's session.

    ``collection`` says whether bars could be expected at all: ``expected``,
    ``unsupported`` (no feed exists), ``not_eligible`` (NXT does not trade the
    symbol), ``eligibility_unknown``, ``symbol_not_in_universe`` or
    ``universe_unread``. Stored bars are evidence and outrank all of them.
    """
    if not bars_read:
        return None, "source_unavailable"
    if bars is not None:
        if bars.receive_lag_seconds > max_lag_seconds:
            return "delayed", "receive_lag_over_threshold"
        if window.end is not None:
            reference = min(as_of, window.end)
            age = (reference - (bars.last_bar_at + _BAR)).total_seconds()
            if age > max_lag_seconds:
                return "delayed", "latest_bar_older_than_threshold"
        if bars.trade_bar_count == 0:
            return "no_trades", "only_zero_volume_bars"
        return "observed", "trade_bars_received"
    if status == "closed":
        return "closed", "not_trading_day"
    if status == "unknown":
        return None, "calendar_unknown"
    if collection == "not_eligible":
        return "closed", "symbol_not_nxt_eligible"
    start = window.start or window.earliest_start
    if start is None:
        return None, "window_unknown"
    if as_of < start:
        return "closed", "session_not_started"
    if collection == "universe_unread":
        return None, "source_unavailable"
    if collection == "eligibility_unknown":
        return None, "eligibility_unknown"
    if collection == "unsupported":
        return "not_collected", "collection_unsupported"
    if window.start is None:
        return None, "window_unknown"
    if as_of < window.start + _BAR + timedelta(seconds=max_lag_seconds):
        # No bar could be late yet: the first one is still inside the allowance.
        return None, "awaiting_first_receipt"
    if collection == "symbol_not_in_universe":
        return "not_collected", "symbol_not_in_universe"
    return "not_collected", "no_stored_bars"


def pick_reference(
    official: Sequence[Mapping[str, Any]] | None,
    daily: Sequence[Mapping[str, Any]] | None,
) -> Reference:
    """KRX official close when present, else the one stored daily close.

    ``None`` = that source was not read. An unread official table never falls
    back to the daily candle: "official absent" would not be proven. US has no
    official source and passes ``official=[]``.
    """
    unread = Reference(None, None, None, "source_unavailable")
    if official is None:
        return unread
    for row in official:
        price = _price(row["close"])
        if price is not None:
            return Reference(price, "krx_official", safe_code(row["source"]), None)
    if daily is None:
        return unread
    if len(daily) > 1:
        # More than one stored row for the session date: no way to choose.
        return Reference(None, None, None, "source_mixed")
    price = _price(daily[0]["close"]) if daily else None
    if price is None:
        return Reference(None, None, None, "no_stored_close")
    return Reference(price, "stored_daily", safe_code(daily[0]["source"]), None)


def _price(value: Any) -> Decimal | None:
    """A stored close as an exact positive decimal; anything else is no close."""
    if value is None:
        return None
    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    return parsed if parsed.is_finite() and parsed > 0 else None


def parse_symbols(value: str, market: str | None) -> list[Subject]:
    """The comma-separated ``symbols`` query: DB symbol form, order kept."""
    if not isinstance(value, str):
        raise RetroReadError("validation_error")
    parts = value.split(",")
    if len(parts) > MAX_SYMBOLS:
        raise RetroReadError("validation_error")
    subjects: dict[tuple[str, str], Subject] = {}
    for part in parts:
        upper = check_symbol(part).upper()
        if FUTURES_SYMBOL.fullmatch(upper) and market in (None, "futures"):
            key, symbol = "futures", upper
        elif market == "futures":
            raise RetroReadError("validation_error")
        else:
            info = (
                infer_market(upper) if market is None else select_market(upper, market)
            )
            if info.key not in MOVER_MARKETS:
                raise RetroReadError("validation_error")
            key, symbol = info.key, normalize_symbol(upper, info)
        subjects.setdefault((key, symbol), Subject(key, symbol, "requested"))
    return list(subjects.values())


# --------------------------------------------------------------------------
# source reads
# --------------------------------------------------------------------------
async def _rows(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[Any]:
    statement = text(sql)
    if "symbols" in params:
        statement = statement.bindparams(bindparam("symbols", expanding=True))
    return list((await session.execute(statement, params)).mappings().all())


async def load_movers(
    session: AsyncSession, market: str, day: date, cap: int
) -> list[str]:
    """The date's ranked symbols, best rank first, then earliest capture."""
    found = await _rows(
        session, _MOVERS_SQL, {"market": market, "day": day, "cap": cap}
    )
    return [str(row["symbol"]) for row in found]


async def load_contracts(session: AsyncSession, day: date) -> list[str]:
    """Futures contracts stored in the last days and not past their month."""
    found = await _rows(
        session,
        _CONTRACTS_SQL,
        {
            "first": day - timedelta(days=FUTURES_LOOKBACK_DAYS),
            "day": day,
            "cap": MAX_SYMBOLS,
        },
    )
    return [
        str(row["symbol"])
        for row in found
        if FUTURES_SYMBOL.fullmatch(str(row["symbol"]))
        and not contract_expired(str(row["symbol"]), day)
    ]


async def load_universe(
    session: AsyncSession, market: str, symbols: Sequence[str]
) -> dict[str, Any]:
    """KR: ``symbol -> nxt_eligible``; US: ``symbol -> exchange`` (active rows)."""
    found = await _rows(session, _UNIVERSE_SQL[market], {"symbols": list(symbols)})
    return {str(row["symbol"]): row["fact"] for row in found}


def _time_bounds(market: str, day: date) -> tuple[datetime, datetime]:
    """``time_utc`` range of a session date, for hypertable chunk exclusion.

    KR is exactly the KST day. US and futures rows are selected by the stored
    ``session_date``; their bounds only need to contain every bar of that date
    (a futures night bar can start before a long holiday).
    """
    if market == "kr":
        return kst_window(day, day)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    back = timedelta(days=2 if market == "us" else FUTURES_NIGHT_REACH_DAYS)
    return midnight - back, midnight + timedelta(days=2)


async def load_session_bars(
    session: AsyncSession, market: str, day: date, symbols: Sequence[str]
) -> list[tuple[BarKey, SessionBars]]:
    params: dict[str, Any] = {"symbols": list(symbols), "cap": MAX_SOURCE_ROWS}
    params["lo"], params["hi"] = _time_bounds(market, day)
    if market != "kr":
        params["day"] = day
    found = await _rows(session, _BARS_SQL[market], params)
    return [
        (
            (str(row["symbol"]), str(row["venue"]), str(row["session_segment"])),
            SessionBars(
                bar_count=int(row["bar_count"]),
                trade_bar_count=int(row["trade_bar_count"]),
                last_bar_at=row["last_bar_at"],
                last_trade_bar_at=row["last_trade_bar_at"],
                last_received_at=row["last_received_at"],
                last_bar_received_at=row["last_bar_received_at"],
                last_bar_source=row["last_bar_source"],
                last_trade_source=row["last_trade_source"],
                sources=tuple(sorted(str(s) for s in row["sources"])),
            ),
        )
        for row in found
    ]


async def load_official_closes(
    session: AsyncSession, day: date, symbols: Sequence[str]
) -> list[Any]:
    return await _rows(
        session, _KR_OFFICIAL_SQL, {"symbols": list(symbols), "day": day}
    )


async def load_daily_closes(
    session: AsyncSession, market: str, day: date, symbols: Sequence[str]
) -> list[Any]:
    """Stored daily rows of one session date (the UTC date of ``time``)."""
    lo = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return await _rows(
        session,
        _DAILY_SQL[market],
        {"symbols": list(symbols), "lo": lo, "hi": lo + timedelta(days=1)},
    )


def _by_symbol(rows: Sequence[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for row in rows:
        out.setdefault(str(row["symbol"]), []).append(row)
    return out


@dataclass(slots=True)
class MarketData:
    """Everything read for one market; ``None`` means that source was not read."""

    status: SessionStatus
    bars: dict[BarKey, SessionBars] | None
    universe: dict[str, Any] | None
    reference_date: date | None
    references: dict[str, Reference]


async def _population(
    session: AsyncSession,
    unknowns: Unknowns,
    market: str,
    day: date,
    movers: int,
) -> tuple[list[Subject], bool]:
    """Default population of one market and whether it was fully read."""
    if market == "futures":
        contracts, ok = await guarded(
            session,
            unknowns,
            "source.futures_contracts",
            lambda: load_contracts(session, day),
            [],
        )
        if ok and not contracts:
            unknowns.note("population.futures", "missing_data")
        return [Subject(market, s, "stored_contract") for s in contracts], ok
    subjects = [Subject(market, s, "fixed") for s in FIXED_SYMBOLS[market]]
    if not movers:
        return subjects, True
    # Extra room so that fixed symbols which also ranked do not use up slots.
    cap = movers + len(subjects)
    found, ok = await guarded(
        session,
        unknowns,
        f"source.movers_{market}",
        lambda: load_movers(session, market, day, cap),
        [],
    )
    if ok and not found:
        unknowns.note(f"population.movers_{market}", "missing_data")
    seen = {s.symbol for s in subjects}
    added = 0
    for raw in found:
        symbol = normalize_symbol(raw, MARKETS[market])
        if added == movers:
            break
        if symbol in seen or safe_code(symbol, fallback=None) is None:
            continue
        seen.add(symbol)
        added += 1
        subjects.append(Subject(market, symbol, "mover"))
    return subjects, ok


async def _read_market(
    session: AsyncSession,
    unknowns: Unknowns,
    market: str,
    day: date,
    symbols: list[str],
) -> tuple[MarketData, bool]:
    ok_all = True

    async def read_bars() -> list[tuple[BarKey, SessionBars]]:
        return await load_session_bars(session, market, day, symbols)

    bar_rows, ok = await guarded(
        session, unknowns, f"source.minute_bars_{market}", read_bars, []
    )
    if ok and len(bar_rows) >= MAX_SOURCE_ROWS:
        unknowns.note(f"source.minute_bars_{market}", "incomplete_evidence")
        ok = False
    ok_all &= ok
    bars = dict(bar_rows) if ok else None

    universe: dict[str, Any] | None = None
    references: dict[str, Reference] = {}
    reference_date: date | None = None
    if market in MOVER_MARKETS:
        found, ok = await guarded(
            session,
            unknowns,
            f"source.symbol_universe_{market}",
            lambda: load_universe(session, market, symbols),
            {},
        )
        ok_all &= ok
        universe = found if ok else None

        reference_date = previous_session_date(market, day)
        if reference_date is None:
            unknown = Reference(None, None, None, "calendar_unknown")
            references = dict.fromkeys(symbols, unknown)
        else:
            previous = reference_date
            official: dict[str, list[Any]] | None = {}
            if market == "kr":
                rows, ok = await guarded(
                    session,
                    unknowns,
                    "source.krx_official_daily",
                    lambda: load_official_closes(session, previous, symbols),
                    [],
                )
                official = _by_symbol(rows) if ok else None
            rows, ok = await guarded(
                session,
                unknowns,
                f"source.daily_candles_{market}",
                lambda: load_daily_closes(session, market, previous, symbols),
                [],
            )
            daily = _by_symbol(rows) if ok else None
            references = {
                symbol: pick_reference(
                    None if official is None else official.get(symbol, []),
                    None if daily is None else daily.get(symbol, []),
                )
                for symbol in symbols
            }
    status = day_status(market, day)
    return MarketData(status, bars, universe, reference_date, references), ok_all


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------
def _slots(subject: Subject, data: MarketData) -> list[tuple[str | None, str, str]]:
    """``(venue, session, collection)`` rows of one symbol, in display order."""
    symbol = subject.symbol
    stored = sorted(
        (venue, session)
        for found, venue, session in (data.bars or {})
        if found == symbol and session in SESSION_ORDER
    )
    slots: dict[tuple[str | None, str], str] = {}
    if subject.market == "kr":
        for venue, sessions in KR_SESSIONS.items():
            for session, _start, _end in sessions:
                collection = "expected"
                if venue == "NTX":
                    if data.universe is None:
                        collection = "universe_unread"
                    elif symbol not in data.universe:
                        collection = "eligibility_unknown"
                    elif data.universe[symbol] is not True:
                        collection = "not_eligible"
                slots[(venue, session)] = collection
    elif subject.market == "us":
        exchange = (data.universe or {}).get(symbol)
        resolved = exchange if exchange in US_EXCHANGES else None
        # The venue the bars were stored under wins over today's universe row.
        venues: list[str | None] = sorted(
            {
                venue
                for venue, _ in stored
                if venue not in (TOSS_US_VENUE, KIS_DAY_US_VENUE)
            }
        ) or [resolved]
        for venue in venues:
            collection = "expected"
            if venue is None:
                collection = (
                    "universe_unread"
                    if data.universe is None
                    else "symbol_not_in_universe"
                )
            for session in US_EXCHANGE_SESSIONS:
                slots[(venue, session)] = collection
        slots[(TOSS_US_VENUE, US_DAY)] = "expected"
    else:
        slots[(FUTURES_VENUE, FUTURES_SESSIONS[0])] = "expected"
        # No night quote or minute feed exists (hk 6148).
        slots[(FUTURES_VENUE, FUTURES_SESSIONS[1])] = "unsupported"
    for venue, session in stored:
        slots.setdefault((venue, session), "expected")
    return sorted(
        ((venue, session, found) for (venue, session), found in slots.items()),
        key=lambda slot: (SESSION_ORDER.index(slot[1]), slot[0] or ""),
    )


def _row(
    subject: Subject,
    venue: str | None,
    session: str,
    collection: str,
    data: MarketData,
    day: date,
    as_of: datetime,
    max_lag_seconds: int,
) -> Row:
    window = (
        session_window(subject.market, session, day, subject.symbol)
        if data.status == "open"
        else UNKNOWN_WINDOW
    )
    bars_read = data.bars is not None
    bars = (
        data.bars.get((subject.symbol, venue, session))
        if data.bars is not None and venue is not None
        else None
    )
    state, reason = derive_state(
        status=data.status,
        window=window,
        bars=bars,
        bars_read=bars_read,
        collection=collection,
        as_of=as_of,
        max_lag_seconds=max_lag_seconds,
    )
    reference = data.references.get(subject.symbol) or Reference(
        None, None, None, "unsupported"
    )
    source = None
    lag = None
    if bars is not None:
        source = safe_code(bars.last_trade_source or bars.last_bar_source)
        # Rounded up, so a row shown as late never prints the threshold itself.
        lag = math.ceil(bars.receive_lag_seconds)
    row: dict[str, Any] = {
        "market": subject.market,
        "symbol": subject.symbol,
        "origin": subject.origin,
        "venue": venue,
        "session": session,
        "session_start": iso(window.start),
        "session_end": iso(window.end),
        "phase": phase_of(window, data.status, as_of),
        "state": state,
        "reason": reason,
        "source": source,
        "sources": (
            None
            if not bars_read
            else [safe_code(s) for s in (bars.sources if bars else ())]
        ),
        "stored_bar_count": (
            None if not bars_read else (bars.bar_count if bars else 0)
        ),
        "trade_bar_count": (
            None if not bars_read else (bars.trade_bar_count if bars else 0)
        ),
        "last_bar_at": iso(bars.last_bar_at) if bars else None,
        "last_trade_bar_at": iso(bars.last_trade_bar_at) if bars else None,
        "last_received_at": iso(bars.last_received_at) if bars else None,
        "receive_lag_seconds": lag,
        "reference_close_price": dec(reference.price),
        # The session date looked up, also when no close was found for it.
        "reference_close_date": (
            data.reference_date.isoformat() if data.reference_date else None
        ),
        "reference_close_basis": reference.basis,
        "reference_close_source": reference.source,
        "reference_close_reason": reference.reason,
        "currency": CURRENCY.get(subject.market),
    }
    return row, {}


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _bounded(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetroReadError("validation_error")
    if not low <= value <= high:
        raise RetroReadError("validation_error")
    return value


async def read_session_observation(
    session: AsyncSession,
    *,
    date: Any,
    market: str | None = None,
    symbols: str | None = None,
    movers: int | None = None,
    max_lag_seconds: int = DEFAULT_MAX_LAG_SECONDS,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Contract ``session_observation``: one row per symbol x venue x session."""
    check_limit(limit)
    day = parse_date(date)
    if market is not None and market not in MARKET_ORDER:
        raise RetroReadError("validation_error", message="Invalid market parameter.")
    if symbols is not None and movers is not None:
        raise RetroReadError("validation_error")
    requested = parse_symbols(symbols, market) if symbols is not None else None
    per_market = 0
    if requested is None:
        per_market = _bounded(
            DEFAULT_MOVERS if movers is None else movers, 0, MAX_MOVERS
        )
    threshold = _bounded(max_lag_seconds, MIN_MAX_LAG_SECONDS, MAX_MAX_LAG_SECONDS)
    digest = params_digest(
        op="session_observation",
        day=day,
        market=market or "",
        symbols=",".join(s.symbol for s in requested or ()),
        movers=per_market,
        max_lag=threshold,
    )
    offset = decode_cursor(cursor, digest)
    unknowns = Unknowns()
    complete = True
    markets: Sequence[str] = (market,) if market else MARKET_ORDER
    collected: list[tuple[list[Subject], MarketData]] = []

    async with read_snapshot(session):
        # Taken inside the snapshot so no row can be newer than ``as_of`` claims.
        as_of = _now()
        for name in markets:
            if requested is None:
                subjects, ok = await _population(
                    session, unknowns, name, day, per_market
                )
                complete &= ok
            else:
                subjects = [s for s in requested if s.market == name]
            if not subjects:
                continue
            data, ok = await _read_market(
                session, unknowns, name, day, [s.symbol for s in subjects]
            )
            complete &= ok
            collected.append((subjects, data))

    rows = [
        _row(subject, venue, name, collection, data, day, as_of, threshold)
        for subjects, data in collected
        for subject in subjects
        for venue, name, collection in _slots(subject, data)
    ]
    data_as_of = latest(
        bars.last_received_at
        for _subjects, data in collected
        for bars in (data.bars or {}).values()
    )
    return {
        **build_response(
            as_of=as_of,
            data_as_of=data_as_of,
            rows=rows,
            total=len(rows) if complete else None,
            limit=limit,
            cursor=cursor,
            digest=digest,
            offset=offset,
            unknowns=unknowns,
        ),
        "rule_parameters": {
            "session_date": day.isoformat(),
            "max_lag_seconds": threshold,
            "movers_per_market": per_market,
        },
    }
