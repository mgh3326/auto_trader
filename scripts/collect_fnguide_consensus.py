#!/usr/bin/env python3
"""hk 6508: FnGuide public CompanyInfo collector (manual CLI, not scheduled here).

    uv run python -m scripts.collect_fnguide_consensus \\
        [--symbols 005930,000660,028300 | --limit N] \\
        [--pages Consensus,Invest,Finance] [--dry-run | --commit] \\
        [--concurrency 2] [--interval 1.0] [--retries 2]

Source: public, login-free ``wcomp.fnguide.com/CompanyInfo/<page>?cmp_cd=<code>``
(the ``comp.fnguide.com`` URL answers 302 to it). Operator decision
``decision/2026-11-25/at-public-scraping-fnguide``. Requests run at most two at a
time and about one per second overall; a generic User-Agent is sent.

Default universe is active KR stocks on KOSPI/KOSDAQ (``kr_symbol_universe``,
``security_type='STOCK'``). ``--symbols`` skips the database entirely, so a dry run
with explicit symbols needs no DB. ``--dry-run`` (default) fetches and parses only.
``--commit`` writes one transaction per page; a same-day rerun writes zero rows.

Output: one JSON object on stdout (per-page status, reason, counts; failures by
closed kind and HTTP status only, no response bodies). Each page is committed as
it completes, so a run cut short keeps what it already stored. Exit 0 = clean,
3 = attention (isolated page failures; some pages parsed, the DAG logs and exits 0),
1 = write error, or nothing parsed, 2 = usage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
from collections.abc import AsyncIterator
from typing import Any

from app.core.timezone import today_kst
from app.services.fnguide_consensus.client import MAX_CONCURRENCY, FnguideClient
from app.services.fnguide_consensus.collector import (
    CollectResult,
    PageSink,
    collect_pages,
    validate_pages,
)
from app.services.fnguide_consensus.parser import PAGES, ParsedPage

_CODE_RE = re.compile(r"[0-9]{6}")


def _parse_symbols(raw: str) -> list[str]:
    symbols = [s.strip() for s in raw.split(",") if s.strip()]
    bad = [s for s in symbols if not _CODE_RE.fullmatch(s)]
    if bad:
        raise ValueError("symbols must be six digits")
    return list(dict.fromkeys(symbols))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--symbols", default="", help="comma-separated six-digit codes")
    source.add_argument(
        "--limit", type=int, default=None, help="first N universe symbols"
    )
    parser.add_argument(
        "--pages", default=",".join(PAGES), help="Consensus,Invest,Finance"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="fetch and parse only (default)"
    )
    mode.add_argument("--commit", action="store_true", help="write snapshots and rows")
    parser.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
    parser.add_argument(
        "--interval", type=float, default=1.0, help="seconds between request starts"
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


async def _load_universe(limit: int | None) -> list[str]:
    from app.core.db import AsyncSessionLocal
    from app.services.fnguide_consensus.universe import load_kr_stock_symbols

    async with AsyncSessionLocal() as session:
        return await load_kr_stock_symbols(session, limit=limit)


@contextlib.asynccontextmanager
async def _page_sink(
    enabled: bool, written: list[dict[str, Any]], write_errors: list[dict[str, Any]]
) -> AsyncIterator[PageSink | None]:
    """Yield a per-page writer (one transaction per page) or None for dry-run."""
    if not enabled:
        yield None
        return
    from app.core.db import AsyncSessionLocal
    from app.services.fnguide_consensus.writer import FnguideSnapshotWriter

    async with AsyncSessionLocal() as session:
        writer = FnguideSnapshotWriter(session)

        async def sink(parsed: ParsedPage) -> None:
            try:
                async with session.begin():
                    outcome = await writer.write_page(parsed)
            except Exception as error:  # noqa: BLE001 - closed kind only
                write_errors.append(
                    {
                        "symbol": parsed.symbol,
                        "page": parsed.page,
                        "kind": type(error).__name__,
                    }
                )
                return
            written.append(
                {
                    "symbol": parsed.symbol,
                    "page": parsed.page,
                    "inserted": outcome.inserted,
                    "value_rows_written": outcome.value_rows,
                    "report_rows_written": outcome.report_rows,
                }
            )

        yield sink


def _exit_code(collected: CollectResult, write_errors: list[dict[str, Any]]) -> int:
    """0 clean; 3 attention (isolated page failures, some pages parsed); 1 failure."""
    if write_errors:
        return 1
    if not collected.summaries:
        return 1  # nothing parsed at all (failures only, or an empty universe)
    if collected.failures:
        return 3
    return 0


async def _run(args: argparse.Namespace) -> int:
    pages = validate_pages(args.pages.split(","))
    if args.symbols:
        symbols = _parse_symbols(args.symbols)
        universe_source = "explicit"
    else:
        symbols = await _load_universe(args.limit)
        universe_source = "kr_symbol_universe"
    observed = today_kst()

    written: list[dict[str, Any]] = []
    write_errors: list[dict[str, Any]] = []
    async with _page_sink(args.commit, written, write_errors) as sink:
        async with FnguideClient(
            min_interval=args.interval,
            concurrency=args.concurrency,
            retries=args.retries,
            timeout=args.timeout,
        ) as client:
            collected = await collect_pages(
                client, symbols, pages, observed, on_parsed=sink
            )

    report = {
        "mode": "commit" if args.commit else "dry-run",
        "observed_date": observed.isoformat(),
        "universe": universe_source,
        "symbols": len(symbols),
        "pages_requested": list(pages),
        "parsed": collected.summaries,
        "failures": [
            {
                "symbol": f.symbol,
                "page": f.page,
                "kind": f.kind,
                "http_status": f.http_status,
            }
            for f in collected.failures
        ],
        "written": written,
        "write_errors": write_errors,
        "totals": {
            "parsed_pages": len(collected.summaries),
            "no_values_pages": sum(
                1 for s in collected.summaries if s["status"] == "no_values"
            ),
            "value_rows": sum(s["value_rows"] for s in collected.summaries),
            "report_rows": sum(s["report_rows"] for s in collected.summaries),
            "failed_pages": len(collected.failures),
        },
    }
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return _exit_code(collected, write_errors)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ValueError as error:
        print(f"usage error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
