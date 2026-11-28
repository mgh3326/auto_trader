"""``retro_symbol_events`` — one symbol's daily bars and official events, as known.

hk 6410, design ``docs/design/6400-official-event-path.md`` §4. One timeline of
stored daily bars and stored official events for one symbol, cut off at
``known_cutoff``: only what we had received by that instant is shown.
``as_of`` stays the generation time (retro/v1 rule).

Cutoff rules (nothing after the cutoff is returned, only counted):

* an event row needs ``first_received_at <= known_cutoff``; its representation
  is the version of ``symbol_event_versions``;
* a daily bar has no first-receipt column, so its proof is the last stored write:
  ``session end <= ingested_at <= known_cutoff``. A bar written again after the
  cutoff is withheld (the value then held cannot be proven);
* a row whose receipt, version, symbol link or bar finality cannot be proven is
  counted in ``excluded_unproven``, never shown and never counted as zero.

``collection`` is the ingestion state at generation time, not at the cutoff: a
partition row is overwritten by later runs, so its past state is not stored. Its
projection is shared with the operator read
(:mod:`app.services.market_events.collection`).

Read-only: SELECT inside the reader's own ``REPEATABLE READ READ ONLY`` snapshot,
every source in a SAVEPOINT. No broker call and no wait for any collector.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import and_, exists, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_kis_symbol, to_yahoo_symbol
from app.models.market_events import (
    MarketEvent,
    MarketEventRevision,
    MarketEventSource,
)
from app.services.market_events import collection as coverage
from app.services.market_events.collection import (
    FAILURE_CODES as FAILURE_CODES,
)
from app.services.market_events.collection import (
    PARTITIONED_SOURCES,
    SCOPED_SOURCES,
)
from app.services.market_events.session_calendar import (
    regular_session_bounds,
    trading_session_status,
)
from app.services.retro_v1.daily_bars import Bar
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    KST,
    MAX_SOURCE_ROWS,
    RetroReadError,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_span,
    check_symbol,
    decode_cursor,
    guarded,
    iso,
    latest,
    params_digest,
    parse_date,
    read_snapshot,
    resolve_from,
    safe_code,
)
from app.services.retro_v1.get_bars import bar_row
from app.services.retro_v1.market import (
    MarketInfo,
    normalize_symbol,
    select_market,
    symbol_for_market,
)
from app.services.retro_v1.symbol_event_versions import (
    REVISIONED_SOURCES,
    SOURCES_BY_MARKET,
    StoredEvent,
    StoredRevision,
    Version,
    resolve_version,
)

_CUTOFF_RE: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
#: KR daily bars move until the KRX and NXT after-markets close (20:00 KST).
_KR_BAR_END: Final = time(20, 0)
_ONE_DAY: Final = timedelta(days=1)

_STATUSES: Final = frozenset(
    {"scheduled", "released", "revised", "cancelled", "tentative"}
)
_RELEVANCE: Final = frozenset({"direct", "sector", "market"})
_RELEVANCE_STATE: Final = frozenset({"known", "unknown"})
_RELEVANCE_BASIS: Final = frozenset({"recorded", "inferred"})
_PRECISIONS: Final = frozenset({"date", "datetime", "minute", "second"})
_OFFSET_RE: Final = re.compile(r"^[A-Za-z0-9_+\-:/]{1,64}$")
_SYMBOL_OUT_RE: Final = re.compile(r"^[A-Za-z0-9_.:/\-]{1,32}$")
_PROVIDER_ID_RE: Final = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_URL_RE: Final = re.compile(
    r"^https://[A-Za-z0-9.\-]+(?:/[A-Za-z0-9_.~\-/%]*)?(?:\?[A-Za-z0-9_.~\-=&%]*)?$"
)
_URL_HOSTS: Final = {
    "dart": frozenset({"dart.fss.or.kr"}),
    "sec": frozenset({"www.sec.gov"}),
    "upbit_notice": frozenset({"upbit.com"}),
    "bithumb_notice": frozenset(
        {"api.bithumb.com", "feed.bithumb.com", "www.bithumb.com"}
    ),
    "benzinga": frozenset({"www.benzinga.com", "benzinga.com"}),
}
_SECRET_QUERY_RE: Final = re.compile(r"token|key|secret|auth|sig|pass", re.IGNORECASE)

_OFFICIAL_SOURCES: Final = frozenset(
    name for names in SOURCES_BY_MARKET.values() for name in names
)
#: The official sources whose collectors write ingestion partitions.
_PARTITIONED: Final = PARTITIONED_SOURCES & _OFFICIAL_SOURCES
#: Run-scoped sources and proven-zero cutovers; the shared projection takes the
#: values this module holds at call time.
_SCOPED: Final = SCOPED_SOURCES
PROVEN_ZERO_CUTOVERS: Final[Mapping[str, datetime]] = coverage.PROVEN_ZERO_CUTOVERS

_BAR_SQL: Final = {
    "kr": """
        SELECT time, symbol, venue AS venue, open, high, low, close, volume,
               source, ingested_at
        FROM public.kr_candles_1d
        WHERE symbol = :symbol AND time >= :lo AND time < :hi
        ORDER BY time, venue
        LIMIT :cap
    """,
    "us": """
        SELECT time, symbol, exchange AS venue, open, high, low, close, volume,
               source, ingested_at
        FROM public.us_candles_1d
        WHERE symbol = :symbol AND time >= :lo AND time < :hi
        ORDER BY time, exchange
        LIMIT :cap
    """,
    "crypto": """
        SELECT c.time, ci.venue_symbol AS symbol, ci.venue AS venue,
               c.open, c.high, c.low, c.close, c.base_volume AS volume,
               c.source, c.ingested_at
        FROM public.crypto_candles_1d AS c
        JOIN public.crypto_instruments AS ci ON ci.id = c.instrument_id
        WHERE ci.venue_symbol = :symbol AND c.time >= :lo AND c.time < :hi
          AND c.is_closed = TRUE
        ORDER BY c.time, ci.venue
        LIMIT :cap
    """,
}


def parse_cutoff(value: object) -> datetime:
    """``known_cutoff``: an RFC 3339 UTC instant ending in ``Z`` (or aware UTC)."""
    if isinstance(value, datetime):
        if value.utcoffset() != timedelta(0):
            raise RetroReadError("validation_error")
        return value.astimezone(UTC)
    if isinstance(value, str) and _CUTOFF_RE.fullmatch(value):
        try:
            return datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            pass
    raise RetroReadError("validation_error")


def stored_spellings(symbol: str, market: MarketInfo) -> tuple[str, ...]:
    """The spellings of one symbol that the event writers store."""
    if market.key == "us":
        found = (symbol, to_yahoo_symbol(symbol), to_kis_symbol(symbol))
    elif market.key == "crypto":
        # Exchange notices store the base asset (``BTC``), not the market key.
        found = (symbol, symbol.partition("-")[2])
    else:
        found = (symbol,)
    return tuple(dict.fromkeys(s for s in found if s))


def safe_url(source: str, url: str | None) -> str | None:
    """The original document link, or ``None`` when it is not a known public one."""
    if not url or len(url) > 512 or not _URL_RE.fullmatch(url):
        return None
    parts = urlsplit(url)
    # ``netloc`` must be the bare allowlisted host: no userinfo and no port.
    if parts.netloc not in _URL_HOSTS.get(source, frozenset()):
        return None
    if any(_SECRET_QUERY_RE.search(name) for name, _ in parse_qsl(parts.query)):
        return None
    return url


def event_ref(event_uuid: Any) -> str:
    """Same opaque ref as ``MarketEvent.event_ref``."""
    return "evt-" + hashlib.sha256(event_uuid.bytes).hexdigest()[:20]


# --------------------------------------------------------------------------
# bars
# --------------------------------------------------------------------------
def session_date(value: datetime, market: MarketInfo) -> date:
    """The market's own calendar date of a stored daily bar."""
    zone = KST if market.key == "kr" else UTC
    return value.astimezone(zone).date()


def bar_end(day: date, market: MarketInfo) -> datetime | None:
    """When the day's bar can no longer move; ``None`` when unknown."""
    if market.key == "crypto":
        return datetime.combine(day + _ONE_DAY, time.min, tzinfo=UTC)
    if market.key == "kr":
        if trading_session_status("kr", day) != "open":
            return None
        return datetime.combine(day, _KR_BAR_END, tzinfo=KST).astimezone(UTC)
    bounds = regular_session_bounds("us", day)
    return None if bounds is None else bounds[1].astimezone(UTC)


def judge_bar(
    end: datetime | None, stored_at: datetime | None, cutoff: datetime
) -> str:
    """``shown``, ``after_cutoff`` or ``unproven`` for one stored daily bar."""
    if stored_at is None:
        return "unproven"
    if stored_at > cutoff:
        return "after_cutoff"
    if end is None:
        return "unproven"
    if end > cutoff:
        return "after_cutoff"
    # Written while the session could still move: not that day's bar.
    return "shown" if stored_at >= end else "unproven"


def _bar_body(
    raw: Any, market: MarketInfo, symbol: str, day: date, end: datetime
) -> Row:
    base, hints = bar_row(
        Bar(
            symbol=symbol,
            time=raw["time"],
            open=raw["open"],
            high=raw["high"],
            low=raw["low"],
            close=raw["close"],
            volume=raw["volume"],
            venue=raw["venue"],
            source=raw["source"],
        ),
        market,
    )
    base.pop("time")
    row = {
        "row_type": "bar",
        "date": day.isoformat(),
        **base,
        "bar_end_at": iso(end),
        "stored_at": iso(raw["ingested_at"]),
        "availability_basis": "last_stored_write",
    }
    return row, hints


# --------------------------------------------------------------------------
# event rows
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    source_event_id: str
    source_url: str | None
    first_received_at: datetime | None


def _member(value: str | None, allowed: frozenset[str]) -> str | None:
    return value if value in allowed else None


def _source_entry(
    source: str, source_event_id: str | None, url: str | None, received: datetime
) -> dict[str, Any]:
    """One provenance entry; a value that cannot be shown has no key."""
    entry: dict[str, Any] = {
        "source": source,
        "first_received_at": iso(received),
    }
    if source_event_id and _PROVIDER_ID_RE.fullmatch(source_event_id):
        entry["source_event_id"] = source_event_id
    link = safe_url(source, url)
    if link is not None:
        entry["source_url"] = link
    return entry


def event_body(
    event: StoredEvent,
    version: Version,
    provenance: Sequence[Provenance],
    market: MarketInfo,
    symbol: str,
    cutoff: datetime,
) -> Row:
    assert event.first_received_at is not None
    entries = [
        _source_entry(p.source, p.source_event_id, p.source_url, p.first_received_at)
        for p in _known_provenance(provenance, cutoff)
    ]
    if not entries:
        entries = [
            _source_entry(
                event.source,
                event.source_event_id,
                version.source_url,
                event.first_received_at,
            )
        ]
    publication = version.publication
    precision = _member(publication.precision, _PRECISIONS)
    offset = publication.offset
    symbols = (
        None
        if version.symbols is None
        else [s for s in version.symbols if _SYMBOL_OUT_RE.fullmatch(s)]
    )
    if version.version_state == "rewritten_after_cutoff":
        reason = "rewritten_after_cutoff"
    elif version.superseded_later:
        reason = "reconstructed_from_revision"
    else:
        reason = None
    row = {
        "row_type": "event",
        "date": version.event_date.isoformat(),
        "symbol": symbol,
        "market": market.key,
        "event_ref": event_ref(event.event_uuid),
        "event_type": safe_code(event.category) or "unclassified",
        "source": event.source,
        "status": _member(version.status, _STATUSES),
        "published_at": iso(publication.at),
        "published_date": publication.day.isoformat() if publication.day else None,
        "published_offset": offset if offset and _OFFSET_RE.fullmatch(offset) else None,
        "published_precision": precision,
        "first_received_at": iso(event.first_received_at),
        "revised_at": iso(version.revised_at),
        "effective_at": iso(version.effective_at),
        "relevance": _member(version.relevance, _RELEVANCE),
        "relevance_state": _member(version.relevance_state, _RELEVANCE_STATE),
        "relevance_basis": _member(version.relevance_basis, _RELEVANCE_BASIS),
        "symbols": symbols,
        "version_seq": version.version_seq,
        "version_state": version.version_state,
        "superseded_later": version.superseded_later,
        "sources": entries,
        "reason": reason,
    }
    hints = {"symbols": "unavailable", "version_seq": "not_applicable"}
    # A date-precision publication has no instant, and the reverse.
    if precision == "date":
        hints["published_at"] = "not_applicable"
    elif precision is not None:
        hints["published_date"] = "not_applicable"
    else:
        for name in (
            "published_at",
            "published_date",
            "published_offset",
            "published_precision",
        ):
            hints[name] = "not_recorded"
    return row, hints


# --------------------------------------------------------------------------
# source reads
# --------------------------------------------------------------------------
def events_statement(
    market: MarketInfo,
    spellings: Sequence[str],
    start: date,
    end: date,
    cutoff: datetime,
) -> Any:
    """Events of the symbol dated in the window, or revised after the cutoff.

    The second arm matters because a later revision may have moved the date: the
    version known at the cutoff can fall in the window when the current row does
    not. The symbol link is read from the current row.
    """
    e, r = MarketEvent, MarketEventRevision
    revised_later = exists().where(
        r.source == e.source,
        r.category == e.category,
        r.market == e.market,
        r.source_event_id == e.source_event_id,
        r.observed_at > cutoff,
    )
    return (
        select(
            e.id,
            e.event_uuid,
            e.source,
            e.category,
            e.market,
            e.source_event_id,
            e.source_url,
            e.symbol,
            e.symbols,
            e.event_date,
            e.status,
            e.first_received_at,
            e.updated_at,
            e.published_at,
            e.published_date,
            e.published_offset,
            e.published_precision,
            e.revised_at,
            e.effective_at,
            e.relevance,
            e.relevance_state,
            e.relevance_basis,
            e.raw_payload_json["rcept_dt"].astext.label("receipt_text"),
            e.raw_payload_json["symbol_resolution_reason"].astext.label(
                "symbol_resolution_reason"
            ),
        )
        .where(
            e.market == market.key,
            e.source.in_(SOURCES_BY_MARKET[market.key]),
            or_(e.symbol.in_(spellings), e.symbols.overlap(list(spellings))),
            or_(and_(e.event_date >= start, e.event_date <= end), revised_later),
        )
        .order_by(e.event_date, e.id)
        .limit(MAX_SOURCE_ROWS)
    )


def _key(row: Any) -> tuple[str, str, str, str]:
    return (row.source, row.category, row.market, row.source_event_id)


async def _read_revisions(
    session: AsyncSession, keys: Sequence[tuple[str, str, str, str]]
) -> list[Any]:
    if not keys:
        return []
    r = MarketEventRevision
    stmt = (
        select(
            r.source,
            r.category,
            r.market,
            r.source_event_id,
            r.revision_seq,
            r.observed_at,
            r.provider_created_at,
            r.raw_payload_json,
        )
        .where(
            tuple_(r.source, r.category, r.market, r.source_event_id).in_(list(keys))
        )
        .order_by(r.source, r.category, r.market, r.source_event_id, r.revision_seq)
        .limit(MAX_SOURCE_ROWS)
    )
    return list((await session.execute(stmt)).all())


async def _read_provenance(session: AsyncSession, ids: Sequence[int]) -> list[Any]:
    if not ids:
        return []
    s = MarketEventSource
    stmt = (
        select(
            s.event_id,
            s.source,
            s.source_event_id,
            s.source_url,
            s.first_received_at,
        )
        .where(s.event_id.in_(list(ids)))
        .order_by(s.event_id, s.source, s.source_event_id)
        .limit(MAX_SOURCE_ROWS)
    )
    return list((await session.execute(stmt)).all())


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Counts:
    """``None`` means the count could not be made; it is never a stand-in for 0."""

    after_cutoff: int | None = 0
    unproven: int | None = 0

    def add(self, verdict: str) -> None:
        if verdict == "after_cutoff" and self.after_cutoff is not None:
            self.after_cutoff += 1
        elif verdict == "unproven" and self.unproven is not None:
            self.unproven += 1

    def lose(self) -> None:
        self.after_cutoff = None
        self.unproven = None


Timed = tuple[tuple[date, datetime, int, str], Row, datetime]
"""``(sort key, row, newest stored instant behind the row)``."""


def _known_provenance(
    provenance: Sequence[Provenance], cutoff: datetime
) -> list[Provenance]:
    """The official provenance rows received by the cutoff: the ones shown."""
    return [
        p
        for p in provenance
        if p.source in _OFFICIAL_SOURCES
        and p.first_received_at is not None
        and p.first_received_at <= cutoff
    ]


async def _events(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    symbol: str,
    start: date,
    end: date,
    cutoff: datetime,
) -> tuple[list[Timed], Counts, bool]:
    spellings = stored_spellings(symbol, market)
    counts = Counts()

    async def read() -> list[Any]:
        stmt = events_statement(market, spellings, start, end, cutoff)
        return list((await session.execute(stmt)).all())

    stored, ok = await guarded(session, unknowns, "source.events", read, [])
    complete = ok
    if ok and len(stored) >= MAX_SOURCE_ROWS:
        unknowns.note("source.events", "incomplete_evidence")
        complete = False

    received: list[StoredEvent] = []
    for raw in stored:
        event = StoredEvent(**{f: getattr(raw, f) for f in StoredEvent.__slots__})
        in_window = start <= event.event_date <= end
        if event.first_received_at is None:
            if in_window:
                counts.add("unproven")
        elif event.first_received_at > cutoff:
            if in_window:
                counts.add("after_cutoff")
        else:
            received.append(event)

    keys = [_key(e) for e in received if e.source_event_id]

    async def revision_rows() -> list[Any]:
        return await _read_revisions(session, keys)

    found, revisions_ok = await guarded(
        session, unknowns, "source.event_revisions", revision_rows, []
    )
    if revisions_ok and len(found) >= MAX_SOURCE_ROWS:
        unknowns.note("source.event_revisions", "incomplete_evidence")
        revisions_ok = False
    revisions: dict[tuple[str, str, str, str], list[StoredRevision]] = defaultdict(list)
    for r in found:
        revisions[_key(r)].append(
            StoredRevision(
                revision_seq=r.revision_seq,
                observed_at=r.observed_at,
                provider_created_at=r.provider_created_at,
                raw_payload=r.raw_payload_json,
            )
        )

    shown: list[tuple[StoredEvent, Version]] = []
    for event in received:
        if not revisions_ok and event.source in REVISIONED_SOURCES:
            # Without the whole log a later revision cannot be ruled out.
            complete = False
            continue
        log = revisions.get(_key(event), []) if event.source_event_id else []
        version = resolve_version(event, log, cutoff, spellings)
        if version is None:
            counts.add("unproven")
        elif start <= version.event_date <= end:
            shown.append((event, version))

    async def provenance_rows() -> list[Any]:
        return await _read_provenance(session, [e.id for e, _ in shown])

    sources, sources_ok = await guarded(
        session, unknowns, "source.event_sources", provenance_rows, []
    )
    if sources_ok and len(sources) >= MAX_SOURCE_ROWS:
        unknowns.note("source.event_sources", "incomplete_evidence")
        sources_ok = False
    provenance: dict[int, list[Provenance]] = defaultdict(list)
    for p in sources:
        provenance[p.event_id].append(
            Provenance(p.source, p.source_event_id, p.source_url, p.first_received_at)
        )

    rows: list[Timed] = []
    for event, version in shown:
        entries = provenance.get(event.id, [])
        body = event_body(event, version, entries, market, symbol, cutoff)
        received_at = event.first_received_at
        assert received_at is not None
        key = (version.event_date, received_at, 1, body[0]["event_ref"])
        stored_at = latest(
            [
                received_at,
                version.stored_at,
                *(p.first_received_at for p in _known_provenance(entries, cutoff)),
            ]
        )
        assert stored_at is not None
        rows.append((key, body, stored_at))
    if not complete:
        counts.lose()
    # A cut-off or failed provenance read changes no exclusion count, but the
    # response is incomplete, so no total is claimed.
    return rows, counts, complete and sources_ok


async def _bars(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    symbol: str,
    start: date,
    end: date,
    cutoff: datetime,
) -> tuple[list[Timed], Counts, bool]:
    counts = Counts()
    # Widen by a day on each side, then keep by the market's own session date.
    lo = datetime.combine(start - _ONE_DAY, time.min, tzinfo=UTC)
    hi = datetime.combine(end + 2 * _ONE_DAY, time.min, tzinfo=UTC)

    async def read() -> list[Any]:
        result = await session.execute(
            text(_BAR_SQL[market.key]),
            {"symbol": symbol, "lo": lo, "hi": hi, "cap": MAX_SOURCE_ROWS},
        )
        return list(result.mappings().all())

    stored, ok = await guarded(session, unknowns, "source.bars", read, [])
    complete = ok
    if ok and len(stored) >= MAX_SOURCE_ROWS:
        unknowns.note("source.bars", "incomplete_evidence")
        complete = False
    rows: list[Timed] = []
    in_window = 0
    for raw in stored:
        day = session_date(raw["time"], market)
        if not start <= day <= end:
            continue
        in_window += 1
        close_at = bar_end(day, market)
        verdict = judge_bar(close_at, raw["ingested_at"], cutoff)
        if verdict != "shown":
            counts.add(verdict)
            continue
        assert close_at is not None
        body = _bar_body(raw, market, symbol, day, close_at)
        key = (day, close_at, 0, str(raw["venue"]))
        rows.append((key, body, raw["ingested_at"]))
    if complete and not in_window:
        # No stored bar at all is a gap, not a day without trading.
        unknowns.note("bars", "missing_data")
    if not complete:
        counts.lose()
    return rows, counts, complete


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------
def collection_gaps(
    partitions: Sequence[Any],
    names: Sequence[str],
    start: date,
    end: date,
    today: date,
) -> list[dict[str, str]]:
    """Closed-reason gaps under this module's cutovers and scoped sources."""
    return coverage.collection_gaps(
        partitions,
        names,
        start,
        end,
        today,
        cutovers=PROVEN_ZERO_CUTOVERS,
        scoped=_SCOPED,
    )


async def _collection(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    start: date,
    end: date,
    today: date,
) -> dict[str, Any]:
    """The shared projection (:mod:`app.services.market_events.collection`)."""
    names = SOURCES_BY_MARKET[market.key]

    async def read() -> tuple[list[Any], list[Any]]:
        window, newest = coverage.partition_statements(
            market.key,
            names,
            start,
            end,
            MAX_SOURCE_ROWS,
            cutovers=PROVEN_ZERO_CUTOVERS,
        )
        return (
            list((await session.execute(window)).all()),
            list((await session.execute(newest)).all()),
        )

    value, ok = await guarded(session, unknowns, "collection", read, ([], []))
    partitions, newest = value
    if ok and len(partitions) >= MAX_SOURCE_ROWS:
        unknowns.note("collection", "incomplete_evidence")
        ok = False

    block, notes = coverage.project_collection(
        names=names,
        partitions=partitions,
        newest=newest,
        start=start,
        end=end,
        today=today,
        ok=ok,
        cutovers=PROVEN_ZERO_CUTOVERS,
        scoped=_SCOPED,
    )
    for field, reason in notes:
        unknowns.note(field, reason)
    return block


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------
async def read_retro_symbol_events(
    session: AsyncSession,
    *,
    symbol: str,
    known_cutoff: Any,
    to: Any,
    from_: Any = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    market: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Contract ``retro_symbol_events``; ``from`` as ``from_`` or ``**{"from": d}``."""
    start = resolve_from(from_, extra)
    end = parse_date(to)
    check_span(start, end)
    check_limit(limit)
    cutoff = parse_cutoff(known_cutoff)
    clean = symbol_for_market(check_symbol(symbol), market)
    info = select_market(clean, market)
    normalized = normalize_symbol(clean, info)
    digest = params_digest(
        op="retro_symbol_events",
        market=info.key,
        symbol=normalized,
        cutoff=cutoff,
        start=start,
        end=end,
    )
    offset = decode_cursor(cursor, digest)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)

    async with read_snapshot(session):
        events, event_counts, events_ok = await _events(
            session, unknowns, info, normalized, start, end, cutoff
        )
        bars, bar_counts, bars_ok = await _bars(
            session, unknowns, info, normalized, start, end, cutoff
        )
        collection = await _collection(
            session, unknowns, info, start, end, as_of.astimezone(KST).date()
        )

    excluded = {
        "events": event_counts.after_cutoff,
        "bars": bar_counts.after_cutoff,
    }
    unproven = {"events": event_counts.unproven, "bars": bar_counts.unproven}
    for block, values in (
        ("excluded_after_cutoff", excluded),
        ("excluded_unproven", unproven),
    ):
        for name, value in values.items():
            if value is None:
                unknowns.note(f"{block}.{name}", "count_unavailable")
    for name, value in unproven.items():
        if value:
            unknowns.note(f"{name}.unproven", "incomplete_evidence")

    timeline = sorted([*bars, *events], key=lambda item: item[0])
    rows = [row for _, row, _ in timeline]
    return {
        **build_response(
            as_of=as_of,
            data_as_of=latest(stored for _, _, stored in timeline),
            rows=rows,
            total=len(rows) if events_ok and bars_ok else None,
            limit=limit,
            cursor=cursor,
            digest=digest,
            offset=offset,
            unknowns=unknowns,
        ),
        "symbol": normalized,
        "market": info.key,
        "known_cutoff": iso(cutoff),
        "excluded_after_cutoff": excluded,
        "excluded_unproven": unproven,
        "collection": collection,
    }
