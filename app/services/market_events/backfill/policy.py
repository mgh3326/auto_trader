"""Closed range, quota and producer rules of a backfill request (hk 6409).

Pure: no database, no clock, no network. A request is either fully valid or
refused with one closed code; nothing is trimmed, split or partially run.

Collectors that read only the provider's latest listing and stop at the newest
stored record cannot fill a past window. They are refused by name
(``source_has_no_date_window``) instead of recording a request the collector
would silently not honour.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.core.symbol import to_db_symbol
from app.services.benzinga_calendars.spec import CALENDAR_ENDPOINTS

#: Longest requested window, inclusive of both ends.
MAX_WINDOW_DAYS: Final = 31
MAX_SYMBOLS: Final = 20
#: Per source, per UTC day. Checked when a request is recorded and again when it
#: is run.
REQUESTS_PER_SOURCE_PER_UTC_DAY: Final = 1
RUNS_PER_SOURCE_PER_UTC_DAY: Final = 1
MAX_REASON_CHARS: Final = 500
MAX_ACTOR_CHARS: Final = 128

REASON_CODES: Final = (
    "gap_fill",
    "collection_failure",
    "correction_recheck",
    "new_symbol",
    "retry_after_failure",
    "verification",
)

_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9][A-Z0-9.]{0,14}$")
_MARKET_ZONES: Final = {
    "kr": ZoneInfo("Asia/Seoul"),
    "us": ZoneInfo("America/New_York"),
}


#: Every reason a request or run can be refused for. A refusal writes nothing.
REFUSAL_CODES: Final = frozenset(
    {
        # source and scope
        "source_has_no_date_window",
        "unsupported_source",
        "unsupported_market",
        "unsupported_category",
        "category_required",
        "symbols_not_supported",
        "symbols_required",
        "too_many_symbols",
        "invalid_symbol",
        # window
        "window_reversed",
        "future_date",
        "window_too_long",
        # audit fields
        "invalid_reason_code",
        "invalid_reason",
        "invalid_requested_by",
        # quota and run state
        "request_quota_exceeded",
        "run_quota_exceeded",
        "already_run",
        "run_unresolved",
        # the collector's own environment gate is closed
        "producer_gate_closed",
    }
)


class BackfillRefused(ValueError):
    """A closed refusal. Nothing was written and nothing was run."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in REFUSAL_CODES:
            raise AssertionError(f"unlisted refusal code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SourceSpec:
    source: str
    market: str
    #: ``None`` means the request must name one (Benzinga calendars).
    category: str | None
    #: ``none`` (market-wide collector), ``required`` or ``optional``.
    symbols: str


#: Sources whose existing collector CLI accepts an explicit date window.
BACKFILL_SOURCES: Final[dict[str, SourceSpec]] = {
    spec.source: spec
    for spec in (
        SourceSpec("dart", "kr", "disclosure", "none"),
        SourceSpec("finnhub", "us", "earnings", "none"),
        SourceSpec("sec", "us", "disclosure", "required"),
        SourceSpec("benzinga", "us", None, "optional"),
    )
}
#: Collectors that read only the latest provider listing (no date window).
UNWINDOWED_SOURCES: Final = frozenset(
    {"naver_disclosure", "upbit_notice", "bithumb_notice"}
)


@dataclass(frozen=True)
class BackfillRequest:
    source: str
    market: str
    category: str
    symbols: tuple[str, ...]
    from_date: date
    to_date: date
    reason_code: str
    reason: str
    requested_by: str

    @property
    def request_hash(self) -> str:
        """Identity of the work. The free-text reason and actor are not part of it."""
        key = [
            self.source,
            self.market,
            self.category,
            list(self.symbols),
            self.from_date.isoformat(),
            self.to_date.isoformat(),
            self.reason_code,
        ]
        body = json.dumps(key, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @property
    def request_ref(self) -> str:
        return f"bfr-{self.request_hash[:20]}"


def market_today(market: str, now: datetime) -> date:
    """The market-local date at ``now`` (an aware instant)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(_MARKET_ZONES[market]).date()


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` of the UTC calendar day that contains ``now``."""
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _source_spec(source: str, market: str) -> SourceSpec:
    if source in UNWINDOWED_SOURCES:
        raise BackfillRefused(
            "source_has_no_date_window",
            f"{source} reads only the latest provider listing; no past window "
            "can be requested",
        )
    spec = BACKFILL_SOURCES.get(source)
    if spec is None:
        raise BackfillRefused(
            "unsupported_source",
            f"{source!r}; supported: {sorted(BACKFILL_SOURCES)}",
        )
    if market != spec.market:
        raise BackfillRefused(
            "unsupported_market", f"{source} supports market {spec.market!r} only"
        )
    return spec


def _category(spec: SourceSpec, category: str | None) -> str:
    if spec.category is not None:
        if category not in (None, spec.category):
            raise BackfillRefused(
                "unsupported_category",
                f"{spec.source} collects category {spec.category!r} only",
            )
        return spec.category
    if not category:
        raise BackfillRefused(
            "category_required", f"{spec.source} needs one --category"
        )
    if category not in CALENDAR_ENDPOINTS:
        raise BackfillRefused(
            "unsupported_category",
            f"{category!r}; supported: {sorted(CALENDAR_ENDPOINTS)}",
        )
    return category


def _symbols(spec: SourceSpec, category: str, raw: Sequence[str]) -> tuple[str, ...]:
    cleaned: set[str] = set()
    for value in raw:
        symbol = to_db_symbol(value.strip().upper())
        if not _SYMBOL_RE.match(symbol):
            raise BackfillRefused("invalid_symbol", repr(value))
        cleaned.add(symbol)
    needs = spec.symbols == "required" or (
        spec.source == "benzinga" and CALENDAR_ENDPOINTS[category].requires_symbols
    )
    if cleaned and spec.symbols == "none":
        raise BackfillRefused(
            "symbols_not_supported", f"{spec.source} collects the whole market"
        )
    if needs and not cleaned:
        raise BackfillRefused(
            "symbols_required", f"{spec.source}/{category} needs --symbols"
        )
    if len(cleaned) > MAX_SYMBOLS:
        raise BackfillRefused(
            "too_many_symbols", f"{len(cleaned)} > {MAX_SYMBOLS} per request"
        )
    return tuple(sorted(cleaned))


def check_window(
    *,
    source: str,
    category: str,
    market: str,
    from_date: date,
    to_date: date,
    now: datetime,
) -> None:
    """Range rules, applied at request time and again at run time."""
    if from_date > to_date:
        raise BackfillRefused("window_reversed", f"{from_date} > {to_date}")
    today = market_today(market, now)
    if to_date > today:
        raise BackfillRefused(
            "future_date", f"{to_date} is after the {market} date {today}"
        )
    days = (to_date - from_date).days + 1
    # A Benzinga category may read further back than the requested window; the
    # extra days are collected too, so they count against the same bound.
    if source == "benzinga":
        days += CALENDAR_ENDPOINTS[category].lookback_days
    if days > MAX_WINDOW_DAYS:
        raise BackfillRefused(
            "window_too_long", f"{days} collected days > {MAX_WINDOW_DAYS}"
        )


def build_request(
    *,
    source: str,
    market: str,
    category: str | None,
    symbols: Sequence[str],
    from_date: date,
    to_date: date,
    reason_code: str,
    reason: str,
    requested_by: str,
    now: datetime,
) -> BackfillRequest:
    """Validate and normalize one request, or raise :class:`BackfillRefused`."""
    spec = _source_spec(source, market)
    chosen = _category(spec, category)
    cleaned = _symbols(spec, chosen, symbols)
    check_window(
        source=source,
        category=chosen,
        market=market,
        from_date=from_date,
        to_date=to_date,
        now=now,
    )
    if reason_code not in REASON_CODES:
        raise BackfillRefused(
            "invalid_reason_code", f"{reason_code!r}; one of {list(REASON_CODES)}"
        )
    text = reason.strip()
    if not text or len(text) > MAX_REASON_CHARS:
        raise BackfillRefused(
            "invalid_reason", f"1..{MAX_REASON_CHARS} characters required"
        )
    actor = requested_by.strip()
    if not actor or len(actor) > MAX_ACTOR_CHARS:
        raise BackfillRefused(
            "invalid_requested_by", f"1..{MAX_ACTOR_CHARS} characters required"
        )
    return BackfillRequest(
        source=source,
        market=market,
        category=chosen,
        symbols=cleaned,
        from_date=from_date,
        to_date=to_date,
        reason_code=reason_code,
        reason=text,
        requested_by=actor,
    )


def producer_argv(request: BackfillRequest) -> list[str]:
    """``python -m`` arguments of the existing collector CLI for this request.

    The collectors are the ones already on main; this package adds no fetch or
    write path of its own to ``market_events``.
    """
    window = [
        "--from-date",
        request.from_date.isoformat(),
        "--to-date",
        request.to_date.isoformat(),
    ]
    if request.source in {"dart", "finnhub"}:
        return [
            "scripts.ingest_market_events",
            "--source",
            request.source,
            "--category",
            request.category,
            "--market",
            request.market,
            *window,
        ]
    if request.source == "sec":
        return [
            "scripts.collect_sec_edgar",
            "--symbols",
            *request.symbols,
            *window,
            "--commit",
        ]
    if request.source == "benzinga":
        scope = ["--symbols", ",".join(request.symbols)] if request.symbols else []
        return [
            "scripts.ingest_benzinga_calendars",
            "--category",
            request.category,
            *window,
            *scope,
            "--commit",
        ]
    raise BackfillRefused("unsupported_source", repr(request.source))
