"""Which version of a stored market event was known at a cutoff (hk 6410).

Pure: no database, no clock. ``retro_symbol_events`` loads the current
``market_events`` row and its ``market_event_revisions`` and asks this module
what may be shown for ``known_cutoff``. A value a writer stored after the cutoff
is never returned: either the as-of revision supplies it, or it is withheld with
a closed reason. Nothing here guesses.

Two kinds of source:

* revisioned (``sec``, ``benzinga``, ``upbit_notice``, ``bithumb_notice``): the
  representation is the last revision with ``observed_at <= cutoff``. When no
  later revision exists the current row is that version; otherwise the version
  is rebuilt from the stored revision, and only the fields it proves are shown.
* without a revision log (``dart``, ``naver_disclosure``): the current row is
  the only version. A ``dart`` row written after the cutoff may differ from what
  was known then, so its changeable values are withheld, and a symbol that was
  backfilled later proves no link at all. Naver rows are insert-only and are
  never rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services.benzinga_calendars.normalizers import normalize_row
from app.services.disclosures.timing import (
    PUBLISHED_FROM_DART_RECEIPT,
    StoredDisclosureRow,
    disclosure_timing,
)

#: Official producers read per market. Finnhub, KIS KSD, Benzinga Pro and the
#: economic calendars are not part of this operation.
SOURCES_BY_MARKET: Final[Mapping[str, tuple[str, ...]]] = {
    "kr": ("dart", "naver_disclosure"),
    "us": ("sec", "benzinga"),
    "crypto": ("upbit_notice", "bithumb_notice"),
}
#: Writers that keep a ``market_event_revisions`` log.
REVISIONED_SOURCES: Final = frozenset(
    {"sec", "benzinga", "upbit_notice", "bithumb_notice"}
)
#: Writers that never update a stored row (``ON CONFLICT DO NOTHING``).
INSERT_ONLY_SOURCES: Final = frozenset({"naver_disclosure"})
#: A writer that also updates the current row without appending a revision
#: (SEC co-registrants only union their tickers into ``symbols``).
_ROW_ONLY_WRITERS: Final = frozenset({"sec"})
_NOTICE_SOURCES: Final = frozenset({"upbit_notice", "bithumb_notice"})
#: The same parenthesized-ticker rule the notice collector attributes with.
_NOTICE_TICKER: Final = re.compile(r"\(([A-Z0-9][A-Z0-9]{0,14})\)")
_KST: Final = ZoneInfo("Asia/Seoul")
_EASTERN: Final = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """The columns of one current ``market_events`` row this operation reads."""

    id: int
    event_uuid: Any
    source: str
    category: str
    market: str
    source_event_id: str | None
    source_url: str | None
    symbol: str | None
    symbols: Sequence[str] | None
    event_date: date
    status: str | None
    first_received_at: datetime | None
    updated_at: datetime
    published_at: datetime | None = None
    published_date: date | None = None
    published_offset: str | None = None
    published_precision: str | None = None
    revised_at: datetime | None = None
    effective_at: datetime | None = None
    relevance: str | None = None
    relevance_state: str | None = None
    relevance_basis: str | None = None
    #: DART only, and the only raw values read: ``raw_payload_json.rcept_dt`` and
    #: why the collector could not resolve a symbol.
    receipt_text: str | None = None
    symbol_resolution_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StoredRevision:
    revision_seq: int
    observed_at: datetime
    provider_created_at: datetime | None
    #: Used only to rebuild a superseded version; never part of a response.
    raw_payload: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class Publication:
    at: datetime | None = None
    day: date | None = None
    offset: str | None = None
    precision: str | None = None


@dataclass(frozen=True, slots=True)
class Version:
    """What of one event may be shown for the cutoff."""

    event_date: date
    version_seq: int | None
    #: ``None`` when there is no revision log and the row was written later.
    superseded_later: bool | None
    version_state: str
    source_url: str | None
    status: str | None = None
    publication: Publication = Publication()
    revised_at: datetime | None = None
    effective_at: datetime | None = None
    relevance: str | None = None
    relevance_state: str | None = None
    relevance_basis: str | None = None
    symbols: tuple[str, ...] | None = None


def _row_publication(event: StoredEvent) -> Publication:
    if event.published_precision:
        return Publication(
            event.published_at,
            event.published_date,
            event.published_offset,
            event.published_precision,
        )
    if event.source == "dart" and event.receipt_text:
        # An enriched DART row keeps its receipt time only in the raw payload.
        timing = disclosure_timing(
            None,
            [StoredDisclosureRow(source="dart", receipt_text=event.receipt_text)],
        )
        if timing["published_at_source"] == PUBLISHED_FROM_DART_RECEIPT:
            instant = datetime.fromisoformat(timing["published_at"]).astimezone(UTC)
            return Publication(
                instant,
                None,
                timing["published_offset"],
                timing["published_precision"],
            )
    # No recorded precision, no publication: ``release_time_utc`` and
    # ``event_date`` are never used in its place (design 6400 §4.2, hk 7091).
    return Publication()


def _current_row(
    event: StoredEvent,
    *,
    version_seq: int | None,
    version_state: str,
    with_symbols: bool = True,
) -> Version:
    symbols: tuple[str, ...] | None = None
    if with_symbols:
        if event.symbols:
            symbols = tuple(event.symbols)
        elif event.symbol:
            symbols = (event.symbol,)
    return Version(
        event_date=event.event_date,
        version_seq=version_seq,
        superseded_later=False,
        version_state=version_state,
        source_url=event.source_url,
        status=event.status,
        publication=_row_publication(event),
        revised_at=event.revised_at,
        effective_at=event.effective_at,
        relevance=event.relevance,
        relevance_state=event.relevance_state,
        relevance_basis=event.relevance_basis,
        symbols=symbols,
    )


def _raw_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _rebuilt_notice(
    event: StoredEvent, revision: StoredRevision, candidates: Collection[str]
) -> Version | None:
    created = revision.provider_created_at
    raw = revision.raw_payload or {}
    title = raw.get("title")
    if created is None or not isinstance(title, str):
        return None
    tickers = set(_NOTICE_TICKER.findall(title))
    if len(tickers) != 1 or not tickers & set(candidates):
        return None
    url = raw.get("pc_url") if event.source == "bithumb_notice" else event.source_url
    return Version(
        event_date=created.astimezone(_KST).date(),
        version_seq=revision.revision_seq,
        superseded_later=True,
        version_state="as_of_revision",
        source_url=url if isinstance(url, str) else None,
        status="released",
        # the notice writer records no publication columns for any version
        symbols=tuple(tickers),
    )


def _rebuilt_sec(
    event: StoredEvent, revision: StoredRevision, candidates: Collection[str]
) -> Version | None:
    # The revision records no tickers. Only the first writer's ``symbol`` (which
    # later writes never change) proves the link.
    if event.symbol not in candidates:
        return None
    accepted = revision.provider_created_at
    local = accepted.astimezone(_EASTERN) if accepted else None
    filed = _raw_date((revision.raw_payload or {}).get("filingDate"))
    if filed is None and local is not None:
        filed = local.date()
    if filed is None:
        return None
    if accepted is not None and local is not None:
        stamp = local.strftime("%z")
        publication = Publication(
            accepted.astimezone(UTC), None, f"{stamp[:3]}:{stamp[3:]}", "second"
        )
    else:
        publication = Publication(None, filed, None, "date")
    return Version(
        event_date=filed,
        version_seq=revision.revision_seq,
        superseded_later=True,
        version_state="as_of_revision",
        source_url=event.source_url,
        status="released",
        publication=publication,
    )


def _rebuilt_benzinga(
    event: StoredEvent, revision: StoredRevision, candidates: Collection[str]
) -> Version | None:
    if revision.raw_payload is None:
        return None
    try:
        # The writer's own pure normalizer over the payload that version held.
        normalized = normalize_row(
            event.category,
            dict(revision.raw_payload),
            first_received_at=revision.observed_at,
            fetched_at=revision.observed_at,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if normalized is None:
        return None
    then = normalized.event
    symbol = then.get("symbol")
    if symbol not in candidates:
        return None
    return Version(
        event_date=then["event_date"],
        version_seq=revision.revision_seq,
        superseded_later=True,
        version_state="as_of_revision",
        source_url=then.get("source_url"),
        status=then.get("status"),
        symbols=(symbol,),
    )


def _rebuild(
    event: StoredEvent, revision: StoredRevision, candidates: Collection[str]
) -> Version | None:
    if event.source in _NOTICE_SOURCES:
        return _rebuilt_notice(event, revision, candidates)
    if event.source == "sec":
        return _rebuilt_sec(event, revision, candidates)
    if event.source == "benzinga":
        return _rebuilt_benzinga(event, revision, candidates)
    return None


def resolve_version(
    event: StoredEvent,
    revisions: Sequence[StoredRevision],
    cutoff: datetime,
    candidates: Collection[str],
) -> Version | None:
    """The version of ``event`` known at ``cutoff``, or ``None`` when unproven.

    ``revisions`` is the event's whole revision log; ``candidates`` are the
    stored spellings of the requested symbol. The caller has already checked
    ``first_received_at <= cutoff``. ``None`` means no version, or no link to
    the symbol, can be proven for the cutoff: the event must not be shown.
    """
    if revisions:
        ordered = sorted(revisions, key=lambda r: r.revision_seq)
        known = [r for r in ordered if r.observed_at <= cutoff]
        if not known:
            return None
        as_of = known[-1]
        if len(known) < len(ordered):
            return _rebuild(event, as_of, candidates)
        if event.source in _ROW_ONLY_WRITERS and event.updated_at > cutoff:
            # The row was touched later without a revision: its ticker union
            # may be newer than the cutoff.
            if event.symbol not in candidates:
                return None
            return _current_row(
                event,
                version_seq=as_of.revision_seq,
                version_state="as_of_revision",
                with_symbols=False,
            )
        return _current_row(
            event, version_seq=as_of.revision_seq, version_state="as_of_revision"
        )
    if event.source == "dart" and event.symbol_resolution_reason is not None:
        # Collected without a symbol: a later backfill set it without moving
        # ``updated_at`` (hk 5623), so when the link was made is not recorded.
        return None
    if event.source not in INSERT_ONLY_SOURCES and event.updated_at > cutoff:
        return Version(
            event_date=event.event_date,
            version_seq=None,
            superseded_later=None,
            version_state="rewritten_after_cutoff",
            source_url=event.source_url,
        )
    return _current_row(event, version_seq=None, version_state="unchanged_since_cutoff")
