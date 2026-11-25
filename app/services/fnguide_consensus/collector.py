"""Fetch and parse orchestration for the FnGuide collector (hk 6508).

No database access here: the caller loads the universe and commits the writes.
A failed fetch or a rejected page is returned as a :class:`PageFailure` with a
closed kind; the failure is reported, never stored, so a later rerun the same day
can still fill it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from app.services.fnguide_consensus.client import FetchError, FnguideClient
from app.services.fnguide_consensus.parser import (
    PAGES,
    PageRejected,
    ParsedPage,
    parse_page,
)


@dataclass(frozen=True)
class PageFailure:
    symbol: str
    page: str
    kind: str
    http_status: int | None = None


@dataclass
class CollectResult:
    pages: list[ParsedPage] = field(default_factory=list)
    failures: list[PageFailure] = field(default_factory=list)


def validate_pages(pages: Iterable[str]) -> tuple[str, ...]:
    chosen = tuple(dict.fromkeys(p.strip() for p in pages if p.strip()))
    if not chosen:
        raise ValueError("at least one page is required")
    unknown = [p for p in chosen if p not in PAGES]
    if unknown:
        raise ValueError(f"unsupported pages: {', '.join(unknown)}")
    return chosen


async def _collect_one(
    client: FnguideClient, symbol: str, page: str, observed_date: date
) -> ParsedPage | PageFailure:
    try:
        fetched = await client.fetch_page(page, symbol)
    except FetchError as error:
        return PageFailure(
            symbol=symbol, page=page, kind=error.kind, http_status=error.status
        )
    try:
        return parse_page(
            fetched.text,
            symbol=symbol,
            page=page,
            observed_date=observed_date,
            fetched_at=fetched.fetched_at,
            source_url=fetched.url,
        )
    except PageRejected as rejected:
        return PageFailure(symbol=symbol, page=page, kind=rejected.kind)
    except Exception:  # noqa: BLE001 - closed kind only; never echo parser text
        return PageFailure(symbol=symbol, page=page, kind="parse_error")


async def collect_pages(
    client: FnguideClient,
    symbols: Iterable[str],
    pages: tuple[str, ...],
    observed_date: date,
) -> CollectResult:
    jobs = [(symbol, page) for symbol in symbols for page in pages]
    results = await asyncio.gather(
        *(_collect_one(client, symbol, page, observed_date) for symbol, page in jobs)
    )
    collected = CollectResult()
    for outcome in results:
        if isinstance(outcome, PageFailure):
            collected.failures.append(outcome)
        else:
            collected.pages.append(outcome)
    return collected
