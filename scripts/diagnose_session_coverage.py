#!/usr/bin/env python3
"""hk 6145: READ-ONLY coverage and latency report for the session minute tables.

    uv run python -m scripts.diagnose_session_coverage --market kr|us --date YYYY-MM-DD \\
        [--symbols 005930 000660]

One ``READ ONLY`` transaction with an 8 s statement timeout, SELECT only. Per
symbol x venue x session_segment it reports rows, expected minutes, missing
minutes and p50/p95 of ``retrieved_at - (time_utc + 1 min)``. Rows whose
``batch_id`` marks a backfill/promotion are excluded (their ``retrieved_at`` is
not a first receipt). Writes nothing, registers nothing.

Exit 0 = report printed, 1 = the read failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime

from sqlalchemy import text

from app.core.cli import setup_logging_and_sentry
from app.core.db import engine
from app.services.research_candles.session_coverage import (
    BACKFILL_BATCH_LIKE,
    StoredMinute,
    coverage_report,
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
    parser.add_argument("--market", required=True, choices=("kr", "us"))
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--symbols", nargs="+", default=None)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, object]:
    symbols: list[str] | None = None
    if args.symbols:
        parts = (p for chunk in args.symbols for p in chunk.split(","))
        symbols = sorted(
            {s for s in (normalize_symbol(args.market, p) for p in parts) if s}
        )
    statement = _SELECT_KR if args.market == "kr" else _SELECT_US
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
    return coverage_report(
        stored,
        market=args.market,
        session_date=args.date,
        as_of=now,
        requested_symbols=symbols or (),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="diagnose-session-coverage")

    async def _go() -> int:
        try:
            report = await run(args)
        except Exception as exc:  # noqa: BLE001 - a failed read must be visible
            print(
                json.dumps({"status": "unavailable", "error_class": type(exc).__name__})
            )
            return 1
        finally:
            await engine.dispose()
        print(json.dumps(report, sort_keys=True))
        return 0

    return asyncio.run(_go())


if __name__ == "__main__":
    sys.exit(main())
