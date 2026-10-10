"""#1086 trigger: DART rows of the #1054 target types -> 1-minute collection.

Given newly ingested DART ``market_events`` rows, this selects the filings the
#1054 research scores, resolves each filer to a KRX symbol exactly the way the
research does, and hands one ``CollectionRequest(symbol, D0, D+1)`` per
``(symbol, D0)`` to an injected ``enqueue`` callable.

Type mapping: ``classify_dart_title`` is the #1054 r3 ``cohort_of``
(hk research/2026-09-30/1054-dart-react, Appendix C, v1.2 N5) ported verbatim;
a parity test pins it. Collected cohorts = the research's primary cohorts plus
its descriptive/pending ones, i.e. supply contract mandatory and voluntary,
direct buyback decision, buyback trust-contract decision, provisional
earnings and rights issue. Amendments, subsidiary variants, cancellations and
buyback results are not collected (the research never scores them).

Listed filter and symbol mapping follow the research as well: ``corp_cls`` in
{유, 코} (KOSPI/KOSDAQ), exact ``company_name == kr_symbol_universe.name``
among common shares, ambiguous names skipped (not guessed).

D0 is the first XKRX session on or after the publication date and D+1 the
next session. Together they hold every v1.2 entry bar: intraday filings enter
on D0, and after-hours, weekend or late-intraday filings enter on D+1 at 09:05.

Nothing here registers a schedule, a task or a hook. Wiring (hk 1084 / Q-122)
is a desk step after merge; see the #1086 PR body.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.services.market_events.session_calendar import (
    is_trading_session,
    next_trading_session,
)
from app.services.research_candles.toss_minute_collector import CollectionRequest

# --- #1054 r3 cohort_of, verbatim -----------------------------------------
RE_AMEND = re.compile(r"^\s*\[|정정")
RE_SUB = re.compile(r"자회사의\s*주요경영사항|종속회사의\s*주요경영사항")


def classify_dart_title(title: str) -> str:
    """v1.2 cohorts. Returns a cohort name; AMEND = version filing (never scored)."""
    if RE_AMEND.search(title):
        return "AMEND"
    sub = bool(RE_SUB.search(title))
    if re.search(r"단일판매.?공급계약체결", title):
        return "SUPPLY_VOL" if "자율공시" in title else "SUPPLY_MAND"
    if re.search(r"단일판매.?공급계약해지", title):
        return "SUPPLY_CANCEL"
    if "자기주식취득결정" in title:
        return "BUYBACK_SUBSIDIARY" if sub else "BUYBACK_DIRECT"
    if "자기주식취득신탁계약체결결정" in title:
        return "BUYBACK_SUBSIDIARY" if sub else "BUYBACK_TRUST_NEW"
    if "자기주식취득신탁계약해지결정" in title:
        return "BUYBACK_TRUST_END"
    if "자기주식취득결과보고서" in title:
        return "BUYBACK_DONE"
    if "자기주식소각" in title:
        return "BUYBACK_CANCEL_SHARES"
    if re.search(r"영업\(잠정\)실적|잠정.?실적|매출액또는손익구조", title):
        return "EARNINGS"
    if re.search(r"주요사항보고서\(유상증자결정\)$", title):
        return "RIGHTS"
    return "OTHER"


# --------------------------------------------------------------------------

#: Cohorts whose symbols get D0/D+1 minute bars.
TARGET_COHORTS: frozenset[str] = frozenset(
    {
        "SUPPLY_MAND",
        "SUPPLY_VOL",
        "BUYBACK_DIRECT",
        "BUYBACK_TRUST_NEW",
        "EARNINGS",
        "RIGHTS",
    }
)
#: DART list ``corp_cls`` values the research counts as listed (KOSPI, KOSDAQ).
LISTED_CORP_CLS: frozenset[str] = frozenset({"유", "코"})

_RCEPT_DT_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::\d{2})?"
)
_SYMBOL_RE = re.compile(r"[0-9A-Z]{6}")
_PREFERRED_NAME_RE = re.compile(r"우[A-C]?$|\d우B?$")


@dataclass(frozen=True)
class DartRow:
    rcept_no: str
    title: str
    company_name: str
    corp_cls: str
    rcept_dt: str
    symbol: str | None = None

    @classmethod
    def from_market_event(cls, event: Any) -> DartRow:
        """Build from a ``MarketEvent`` (or any object with its attributes).

        ``rcept_dt`` and ``corp_cls`` come from ``raw_payload_json``: the
        research found ``release_time_*`` NULL on every row and uses the
        payload's minute-precision ``rcept_dt`` as T0.
        """
        payload: Mapping[str, Any] = getattr(event, "raw_payload_json", None) or {}
        return cls(
            rcept_no=str(getattr(event, "source_event_id", "") or ""),
            title=str(getattr(event, "title", "") or ""),
            company_name=str(getattr(event, "company_name", "") or ""),
            corp_cls=str(payload.get("corp_cls") or ""),
            rcept_dt=str(payload.get("rcept_dt") or ""),
            symbol=getattr(event, "symbol", None),
        )


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    is_common_share: bool | None


class SymbolIndex:
    """Exact-name resolver, the #1054 ``map_symbols`` rule.

    Keeps common shares only (``is_common_share`` true, or unknown and the name
    does not look like a preferred share); a name held by more than one kept
    symbol is ambiguous and never resolved.
    """

    def __init__(self, rows: Iterable[UniverseRow]) -> None:
        by_name: dict[str, list[str]] = {}
        for row in rows:
            common = row.is_common_share is True or (
                row.is_common_share is None and not _PREFERRED_NAME_RE.search(row.name)
            )
            if common:
                by_name.setdefault(row.name, []).append(row.symbol)
        self._unique = {n: s[0] for n, s in by_name.items() if len(s) == 1}
        self._ambiguous = {n for n, s in by_name.items() if len(s) > 1}

    def resolve(self, company_name: str) -> tuple[str | None, str | None]:
        """Return ``(symbol, skip_reason)``."""
        if company_name in self._ambiguous:
            return None, "ambiguous_name"
        symbol = self._unique.get(company_name)
        if symbol is None:
            return None, "unmapped_name"
        return symbol, None


def parse_rcept_dt(raw: str) -> datetime | None:
    """KST-naive minute timestamp from the payload ``rcept_dt``; None if absent."""
    match = _RCEPT_DT_RE.match(raw.strip())
    if match is None:
        return None
    return datetime.fromisoformat(
        f"{match['date']}T{match['hour']}:{match['minute']}:00"
    )


def first_session_on_or_after(day: date) -> date | None:
    if is_trading_session("kr", day):
        return day
    return next_trading_session("kr", day)


@dataclass(frozen=True)
class SkippedRow:
    rcept_no: str
    reason: str
    cohort: str | None = None


@dataclass
class TriggerPlan:
    requests: list[CollectionRequest] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)

    def cohort_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for request in self.requests:
            for cohort in request.cohorts:
                counts[cohort] = counts.get(cohort, 0) + 1
        return counts


def plan_collection_requests(
    rows: Iterable[DartRow],
    *,
    symbol_index: SymbolIndex,
    cohorts: frozenset[str] = TARGET_COHORTS,
    session_on_or_after: Callable[[date], date | None] | None = None,
    next_session: Callable[[date], date | None] | None = None,
) -> TriggerPlan:
    """Pure selection: rows -> deduplicated ``(symbol, D0)`` requests.

    The session callables default to the fail-closed XKRX calendar; a date it
    cannot confirm yields a ``no_session`` skip, never a guessed session.
    """
    if session_on_or_after is None:
        session_on_or_after = first_session_on_or_after
    if next_session is None:

        def next_session(day: date) -> date | None:
            return next_trading_session("kr", day)

    unknown = cohorts - TARGET_COHORTS
    if unknown:
        raise ValueError(f"not a #1086 target cohort: {sorted(unknown)}")
    plan = TriggerPlan()
    merged: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        cohort = classify_dart_title(row.title)
        if cohort not in cohorts:
            plan.skipped.append(SkippedRow(row.rcept_no, "non_target_type", cohort))
            continue
        if row.corp_cls not in LISTED_CORP_CLS:
            plan.skipped.append(SkippedRow(row.rcept_no, "not_kospi_kosdaq", cohort))
            continue
        t0 = parse_rcept_dt(row.rcept_dt)
        if t0 is None:
            plan.skipped.append(SkippedRow(row.rcept_no, "no_rcept_time", cohort))
            continue
        if t0.hour == 0 and t0.minute == 0:
            # The research drops 00:00 placeholders as untimed.
            plan.skipped.append(
                SkippedRow(row.rcept_no, "midnight_placeholder", cohort)
            )
            continue
        if row.symbol and _SYMBOL_RE.fullmatch(row.symbol):
            symbol: str | None = row.symbol
        else:
            symbol, reason = symbol_index.resolve(row.company_name)
            if symbol is None:
                plan.skipped.append(SkippedRow(row.rcept_no, str(reason), cohort))
                continue
        d0 = session_on_or_after(t0.date())
        d1 = next_session(d0) if d0 is not None else None
        if d0 is None or d1 is None:
            plan.skipped.append(SkippedRow(row.rcept_no, "no_session", cohort))
            continue
        entry = merged.setdefault(
            (symbol, d0), {"d1": d1, "rcept_nos": [], "cohorts": []}
        )
        if row.rcept_no not in entry["rcept_nos"]:
            entry["rcept_nos"].append(row.rcept_no)
        if cohort not in entry["cohorts"]:
            entry["cohorts"].append(cohort)
    for (symbol, d0), entry in sorted(merged.items()):
        plan.requests.append(
            CollectionRequest(
                symbol=symbol,
                d0=d0,
                d1=entry["d1"],
                rcept_nos=tuple(sorted(entry["rcept_nos"])),
                cohorts=tuple(sorted(entry["cohorts"])),
            )
        )
    return plan


Enqueue = Callable[[CollectionRequest], Awaitable[None]]


async def schedule_collections_for_dart_rows(
    rows: Iterable[DartRow],
    *,
    symbol_index: SymbolIndex,
    enqueue: Enqueue,
    cohorts: frozenset[str] = TARGET_COHORTS,
) -> TriggerPlan:
    """Plan and hand every request to ``enqueue`` (caller-owned transport).

    ``enqueue`` receives requests whose D+1 may not have finished; the
    collector returns ``immature`` for those and the caller retries after
    ``request.ready_at``. This function itself schedules nothing durable.
    """
    plan = plan_collection_requests(rows, symbol_index=symbol_index, cohorts=cohorts)
    for request in plan.requests:
        await enqueue(request)
    return plan


# ---------------------------------------------------------------------------
# read-only loaders (SELECT only)
# ---------------------------------------------------------------------------


async def load_dart_rows(session: Any, start: date, end: date) -> list[DartRow]:
    """DART ``market_events`` rows whose publication date is in [start, end]."""
    from sqlalchemy import select

    from app.models.market_events import MarketEvent

    if end < start:
        raise ValueError("end must be >= start")
    result = await session.execute(
        select(MarketEvent)
        .where(
            MarketEvent.source == "dart",
            MarketEvent.market == "kr",
            MarketEvent.event_date >= start,
            MarketEvent.event_date <= end,
        )
        .order_by(MarketEvent.event_date, MarketEvent.source_event_id)
    )
    return [DartRow.from_market_event(event) for event in result.scalars()]


async def load_symbol_index(session: Any) -> SymbolIndex:
    from sqlalchemy import select

    from app.models.kr_symbol_universe import KRSymbolUniverse

    result = await session.execute(
        select(
            KRSymbolUniverse.symbol,
            KRSymbolUniverse.name,
            KRSymbolUniverse.is_common_share,
        )
    )
    return SymbolIndex(
        UniverseRow(symbol=s, name=n, is_common_share=c) for s, n, c in result.all()
    )
