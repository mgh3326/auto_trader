"""Collection coverage and closed gap reasons over ingestion partitions.

Design ``docs/design/6400-official-event-path.md`` §3.3 (hk 6409, reused by the
hk 6410 retro reader). The block is a projection of the stored
``market_event_ingestion_partitions`` rows at generation time: no new
computation, no collector call, no write.

* ``latest_collected`` — per source, the newest ``succeeded`` partition date
  that stored events (or came from a source with a proven complete zero).
* ``gaps`` — closed reasons only; a stored ``last_error`` message is never
  returned.
* A day is *expected* only for the ``(source, category, market)`` triples of
  :mod:`app.services.market_events.expected_sources`, on that market's exchange
  sessions: a weekend or holiday is not a gap, and a day the calendar cannot
  classify is ``calendar_unknown``, never silently closed.
* A source with no partition writer answers ``null`` plus ``no_partition``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events.expected_sources import EXPECTED_SOURCES
from app.services.market_events.session_calendar import trading_session_status

MAX_GAPS: Final = 200
DEFAULT_ROW_LIMIT: Final = 5000
_ONE_DAY: Final = timedelta(days=1)
_CATEGORY_TOKEN_MAX: Final = 64
_CATEGORY_RE: Final = re.compile(r"^[A-Za-z0-9_.:\-]+$")

#: Closed list of sources whose ``succeeded`` + 0 events proves a complete empty
#: list. No collector proves it yet (design 6400 §3.3, hk 7091).
PROVEN_ZERO_SOURCES: Final[frozenset[str]] = frozenset()
#: Closed failure codes a v2 writer stores in ``last_error``; a copy of
#: ``market_events.repository.CollectionFailure`` (a writer module this read
#: module must not import), pinned by a test.
FAILURE_CODES: Final = frozenset(
    {
        "http_4xx",
        "http_5xx",
        "timeout",
        "transport",
        "bad_response",
        "unparseable",
        "quota",
    }
)
#: Sources whose collectors write ingestion partitions.
PARTITIONED_SOURCES: Final = frozenset(
    {"benzinga", "dart", "finnhub", "forexfactory", "sec", "tradingview", "wisefn"}
)
#: Official-event sources with a collector, per market, for the operator read.
COLLECTOR_SOURCES_BY_MARKET: Final[dict[str, tuple[str, ...]]] = {
    "kr": ("dart", "naver_disclosure"),
    "us": ("finnhub", "sec", "benzinga"),
    "crypto": ("upbit_notice", "bithumb_notice"),
}


def expected_days(names: Sequence[str]) -> dict[str, tuple[str, str]]:
    """``source -> (category, calendar market)`` for the expected ones in ``names``."""
    return {
        source: (category, market)
        for source, category, market in EXPECTED_SOURCES
        if source in names
    }


def partition_gap(row: Any) -> str | None:
    """The closed gap reason of one stored partition, or ``None`` when covered."""
    if row.status == "succeeded":
        if row.event_count > 0 or row.source in PROVEN_ZERO_SOURCES:
            return None
        return "unproven_zero"
    if row.status in {"pending", "running", "partial"}:
        return row.status
    return row.last_error if row.last_error in FAILURE_CODES else "failed"


def _category_token(category: str) -> str:
    """The stored category as a closed code; free text never leaves."""
    if len(category) <= _CATEGORY_TOKEN_MAX and _CATEGORY_RE.match(category):
        return category
    return "unclassified"


def collection_gaps(
    partitions: Sequence[Any],
    names: Sequence[str],
    start: date,
    end: date,
    today: date,
) -> list[dict[str, str]]:
    """Closed-reason gaps of the window from the stored partition rows.

    An expected source's day counts only when its exchange is open (or the
    calendar cannot say), and such a day without a partition is a gap up to
    ``today``. No other source is expected on any day, so only its stored rows
    are judged.
    """
    expected = expected_days(names)
    gaps: list[tuple[str, str, date, str]] = []
    collected: set[tuple[str, date]] = set()
    for row in partitions:
        if row.source in expected:
            category, calendar = expected[row.source]
            if row.category != category:
                continue
            collected.add((row.source, row.partition_date))
            if trading_session_status(calendar, row.partition_date) == "closed":
                continue
        reason = partition_gap(row)
        if reason is not None:
            gaps.append((row.source, row.category, row.partition_date, reason))
    for source, (category, calendar) in expected.items():
        day = start
        while day <= min(end, today):
            if (source, day) not in collected:
                status = trading_session_status(calendar, day)
                if status != "closed":
                    reason = "not_yet_run" if status == "open" else "calendar_unknown"
                    gaps.append((source, category, day, reason))
            day += _ONE_DAY
    return [
        {
            "source": source,
            "category": _category_token(category),
            "date": day.isoformat(),
            "reason": reason,
        }
        for source, category, day, reason in sorted(
            gaps, key=lambda g: (g[2], g[0], g[1])
        )
    ]


def partition_statements(
    market: str,
    names: Sequence[str],
    start: date,
    end: date,
    limit: int,
) -> tuple[Select[Any], Select[Any]]:
    """``(window rows, newest collected date per source)`` — SELECTs only."""
    p = MarketEventIngestionPartition
    scope = (p.market == market, p.source.in_(names))
    window = (
        select(
            p.source,
            p.category,
            p.partition_date,
            p.status,
            p.event_count,
            p.last_error,
        )
        .where(*scope, p.partition_date >= start, p.partition_date <= end)
        .order_by(p.partition_date, p.source, p.category)
        .limit(limit)
    )
    newest = (
        select(p.source, func.max(p.partition_date).label("latest"))
        .where(
            *scope,
            p.status == "succeeded",
            or_(p.event_count > 0, p.source.in_(sorted(PROVEN_ZERO_SOURCES))),
        )
        .group_by(p.source)
        .order_by(p.source)
    )
    return window, newest


def project_collection(
    *,
    names: Sequence[str],
    partitions: Sequence[Any],
    newest: Sequence[Any],
    start: date,
    end: date,
    today: date,
    ok: bool,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """The collection block plus its ``(field, reason)`` unknowns.

    ``ok=False`` (read failed or truncated) yields ``null`` values, never zero or
    an empty gap list.
    """
    found = {row.source: row.latest for row in newest} if ok else {}
    latest_collected: dict[str, str | None] = {}
    unknowns: list[tuple[str, str]] = []
    for name in names:
        day = found.get(name)
        latest_collected[name] = day.isoformat() if day else None
        field = f"collection.latest_collected.{name}"
        if name not in PARTITIONED_SOURCES:
            unknowns.append((field, "no_partition"))
        elif day is None and ok:
            unknowns.append((field, "missing_data"))
    gaps = collection_gaps(partitions, names, start, end, today) if ok else None
    truncated = gaps is not None and len(gaps) > MAX_GAPS
    block = {
        "basis": "current_partitions",
        "latest_collected": latest_collected,
        "gaps": None if gaps is None else gaps[-MAX_GAPS:],
        "gaps_truncated": truncated,
    }
    return block, unknowns


async def read_collection(
    session: AsyncSession,
    *,
    market: str,
    start: date,
    end: date,
    today: date,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> dict[str, Any]:
    """The operator read for one market. The caller owns the (read-only) transaction.

    A read error propagates to the caller: it is never reported as "no gaps".
    """
    if market not in COLLECTOR_SOURCES_BY_MARKET:
        raise ValueError(f"unsupported market: {market!r}")
    if start > end:
        raise ValueError("start must be <= end")
    names = COLLECTOR_SOURCES_BY_MARKET[market]
    window, newest = partition_statements(market, names, start, end, row_limit)
    partitions = list((await session.execute(window)).all())
    latest = list((await session.execute(newest)).all())
    ok = len(partitions) < row_limit
    block, unknowns = project_collection(
        names=names,
        partitions=partitions,
        newest=latest,
        start=start,
        end=end,
        today=today,
        ok=ok,
    )
    if not ok:
        unknowns.append(("collection", "incomplete_evidence"))
    return {
        **block,
        "market": market,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "unknowns": [{"field": f, "reason": r} for f, r in unknowns],
    }
