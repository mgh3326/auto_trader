#!/usr/bin/env python3
"""hk 6145: READ-ONLY coverage and latency report for the session minute tables.

    uv run python -m scripts.diagnose_session_coverage --market kr|us|futures --date YYYY-MM-DD \\
        [--symbols 005930 000660] [--collect-report RUN.json [RUN2.json ...]]
    uv run python -m scripts.diagnose_session_coverage --market us --date YYYY-MM-DD --first-last \\
        [--symbols NVDA]

One ``READ ONLY`` transaction with an 8 s statement timeout, SELECT only. Per
symbol x venue x session_segment it reports rows, expected minutes, missing
minutes and p50/p95 of ``retrieved_at - (time_utc + 1 min)``. Rows whose
``batch_id`` marks a backfill/promotion are excluded (their ``retrieved_at`` is
not a first receipt). Writes nothing, registers nothing.

``--collect-report`` takes the JSON that ``collect_session_minutes`` printed (one
object per line, same market). With it the output lists every run's tier counts
and cut symbols, and each requested symbol with no stored minute gets one closed
reason (hk 6299): ``not_selected``, ``cut_by_budget``, ``fetch_error`` or
``no_trades_reported``. Optional: without it the output is the plain coverage report
(no ``requested_zero_row`` key). Reports of a ``dry_run`` give no evidence.

``--first-last`` (hk 6322, US only) prints, per symbol x source x venue x session
segment of one ET session date, the first and the last stored bar START (UTC and KST).
It is the one-command check for the US day market: Toss rows (``TOSS_INTEGRATED``,
segment ``US_DAY``) sit next to the KIS rows, never merged. Same READ ONLY rules.

Exit 0 = report printed, 1 = the read failed, 2 = usage or an unreadable run report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text

from app.core.cli import setup_logging_and_sentry
from app.core.db import engine
from app.services.research_candles.session_coverage import (
    BACKFILL_BATCH_LIKE,
    EdgeRow,
    StoredMinute,
    coverage_report,
    first_last_report,
    run_evidence_from_report,
)
from app.services.research_candles.session_minutes_collector import (
    normalize_symbol,
)

_SELECT_KR = text(
    "SELECT symbol, venue, session_segment, time_utc, retrieved_at, "
    "(value IS NULL) AS value_is_null "
    "FROM research.kr_candles_1m "
    "WHERE session_date_kst = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[]))) "
    "AND batch_id NOT LIKE :backfill_a AND batch_id NOT LIKE :backfill_b"
)
_SELECT_FUTURES = text(
    "SELECT symbol, 'KRX' AS venue, session_segment, time_utc, retrieved_at, "
    "FALSE AS value_is_null "
    "FROM research.kr_futures_candles_1m "
    "WHERE session_date = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[]))) "
    "AND batch_id NOT LIKE :backfill_a AND batch_id NOT LIKE :backfill_b"
)
_SELECT_US_EDGE = text(
    "SELECT symbol, source, venue, session_segment, time_utc "
    "FROM research.us_candles_1m "
    "WHERE session_date = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[])))"
)
_SELECT_US = text(
    "SELECT symbol, venue, session_segment, time_utc, retrieved_at, "
    "(value IS NULL) AS value_is_null "
    "FROM research.us_candles_1m "
    "WHERE session_date = :session_date "
    "AND (CAST(:symbols AS text[]) IS NULL OR symbol = ANY(CAST(:symbols AS text[]))) "
    "AND batch_id NOT LIKE :backfill_a AND batch_id NOT LIKE :backfill_b"
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--market", required=True, choices=("kr", "us", "futures"))
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument(
        "--first-last",
        action="store_true",
        help="US only: first and last stored bar per symbol, source, venue and segment",
    )
    parser.add_argument(
        "--collect-report",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="collect_session_minutes JSON output (one object per line)",
    )
    args = parser.parse_args(argv)
    if args.first_last and args.market != "us":
        parser.error("--first-last is available for --market us only")
    return args


def _load_run_reports(paths: Sequence[Path], market: str) -> list[dict[str, object]]:
    """Run reports of this market; a malformed line is a usage error (exit 2)."""
    reports: list[dict[str, object]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            report = json.loads(line)
            if not isinstance(report, dict):
                raise ValueError("run report line is not a JSON object")
            run_evidence_from_report(report)  # shape check, raises ValueError
            if report.get("market") == market:
                reports.append(report)
    return reports


def _run_summary(report: dict[str, object]) -> dict[str, object]:
    return {
        "batch_id": report.get("batch_id"),
        "market": report.get("market"),
        "dry_run": report.get("dry_run"),
        "targets_tiers": report.get("targets_tiers"),
        "targets_truncated": report.get("targets_truncated"),
        "targets_cuts": report.get("targets_cuts"),
    }


async def run(
    args: argparse.Namespace, reports: Sequence[dict[str, object]] = ()
) -> dict[str, object]:
    symbols: list[str] | None = None
    if args.symbols:
        parts = (p for chunk in args.symbols for p in chunk.split(","))
        if args.market == "futures":
            # contract symbols like MF200.2612 — used as given, upper-cased
            symbols = sorted({p.strip().upper() for p in parts if p.strip()})
        else:
            symbols = sorted(
                {s for s in (normalize_symbol(args.market, p) for p in parts) if s}
            )
    if args.first_last:
        return await _first_last(args, symbols)
    statement = {"kr": _SELECT_KR, "us": _SELECT_US, "futures": _SELECT_FUTURES}[
        args.market
    ]
    now = datetime.now(UTC)
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
            result = await conn.execute(
                statement,
                {
                    "session_date": args.date,
                    "symbols": symbols,
                    "backfill_a": BACKFILL_BATCH_LIKE[0],
                    "backfill_b": BACKFILL_BATCH_LIKE[1],
                },
            )
            stored = [
                StoredMinute(
                    symbol=row.symbol,
                    venue=row.venue,
                    session_segment=row.session_segment,
                    time_utc=row.time_utc,
                    retrieved_at=row.retrieved_at,
                    value_is_null=bool(row.value_is_null),
                )
                for row in result
            ]
    evidence = [run_evidence_from_report(r) for r in reports] if reports else None
    report = coverage_report(
        stored,
        market=args.market,
        session_date=args.date,
        as_of=now,
        requested_symbols=symbols or (),
        run_evidence=evidence,
    )
    if reports:
        report["runs"] = [_run_summary(r) for r in reports]
    return report


async def _first_last(
    args: argparse.Namespace, symbols: list[str] | None
) -> dict[str, object]:
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
            result = await conn.execute(
                _SELECT_US_EDGE, {"session_date": args.date, "symbols": symbols}
            )
            edges = [
                EdgeRow(
                    symbol=row.symbol,
                    source=row.source,
                    venue=row.venue,
                    session_segment=row.session_segment,
                    time_utc=row.time_utc,
                )
                for row in result
            ]
    return first_last_report(edges, market="us", session_date=args.date)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="diagnose-session-coverage")

    try:
        reports = _load_run_reports(args.collect_report, args.market)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "invalid_run_report", "error_class": type(exc).__name__}
            )
        )
        return 2
    if args.collect_report and not reports:
        print(json.dumps({"status": "no_matching_run_report", "market": args.market}))
        return 2

    async def _go() -> int:
        try:
            report = await run(args, reports)
        except Exception as exc:  # noqa: BLE001 - a failed read must be visible
            print(
                json.dumps({"status": "unavailable", "error_class": type(exc).__name__})
            )
            return 1
        finally:
            await engine.dispose()
        print(json.dumps(report, sort_keys=True, default=str))
        return 0

    return asyncio.run(_go())


if __name__ == "__main__":
    sys.exit(main())
