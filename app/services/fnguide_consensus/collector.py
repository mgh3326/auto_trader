"""Fetch and parse orchestration for the FnGuide collector (hk 6508).

No database access here: the caller supplies ``on_parsed`` to persist each page as
soon as it completes. Pages are not retained, so memory stays bounded and a run
cut short keeps every page already written. A failed fetch or a rejected page is a
:class:`PageFailure` with a closed kind and is never stored, so a later same-day
rerun can still fill it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.fnguide_consensus.client import FetchError, FnguideClient
from app.services.fnguide_consensus.parser import (
    PAGES,
    PageRejected,
    ParsedPage,
    parse_page,
)

PageSink = Callable[[ParsedPage], Awaitable[None]]


class SinkAborted(RuntimeError):
    """The sink gave up (for example after consecutive write failures).

    ``partial`` carries the :class:`CollectResult` gathered before the abort, so the
    caller can still report it. The pending fetch tasks are cancelled, so a database
    outage stops the scraping instead of running out the clock.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.partial: CollectResult | None = None


@dataclass(frozen=True)
class PageFailure:
    symbol: str
    page: str
    kind: str
    http_status: int | None = None


@dataclass
class CollectResult:
    summaries: list[dict[str, Any]] = field(default_factory=list)
    failures: list[PageFailure] = field(default_factory=list)


def summarize_page(parsed: ParsedPage) -> dict[str, Any]:
    return {
        "symbol": parsed.symbol,
        "page": parsed.page,
        "company_name": parsed.company_name,
        "status": parsed.status,
        "reason": parsed.reason,
        "value_rows": parsed.value_count,
        "report_rows": parsed.report_count,
        "skipped_items": parsed.skipped_items,
        "unparsed_blocks": parsed.unparsed_blocks,
        "duplicate_keys": parsed.duplicate_keys,
        "raw_reason": parsed.raw_reason,
    }


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
    except Exception:  # noqa: BLE001 - one bad page must not abort the run; kind only
        return PageFailure(symbol=symbol, page=page, kind="unexpected")
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
    on_parsed: PageSink | None = None,
) -> CollectResult:
    """Fetch and parse every (symbol, page); ``on_parsed`` persists each page as it completes."""
    tasks = [
        asyncio.ensure_future(_collect_one(client, symbol, page, observed_date))
        for symbol in symbols
        for page in pages
    ]
    result = CollectResult()
    try:
        for finished in asyncio.as_completed(tasks):
            outcome = await finished
            if isinstance(outcome, PageFailure):
                result.failures.append(outcome)
                continue
            result.summaries.append(summarize_page(outcome))
            if on_parsed is not None:
                try:
                    await on_parsed(outcome)
                except SinkAborted as aborted:
                    aborted.partial = result
                    raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    return result
