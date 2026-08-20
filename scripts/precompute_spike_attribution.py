#!/usr/bin/env python
"""ROB-1303 phase 2 — pre-open precompute / intraday incremental refresh.

    # pre-open (operator runs this; KR 07:30 / US 21:30 KST per the issue)
    uv run python -m scripts.precompute_spike_attribution \
        --date 2026-08-20 --mode preopen --all --limit 200

    # intraday, 15-minute cadence
    uv run python -m scripts.precompute_spike_attribution \
        --date 2026-08-20 --mode intraday --symbol 035420 --symbol 035720

🔴 **No schedule is registered.** This is an operator-invoked entry point; the
cadence in ``--mode`` only stamps the freshness yardstick onto each entry. Cron
/ TaskIQ / Prefect wiring is deliberately out of scope (ROB-1303 AC3 says a new
cron is forbidden — the pre-open run joins the ROB-1297 digest cron later).

Writes cache files only. No DB row, no broker call, no order/watch surface.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.services.spike_attribution.cache import MODE_INTRADAY, MODE_PREOPEN, cache_dir
from app.services.spike_attribution.materials import (
    DAILY_TABLE_BY_MARKET,
    SESSION_TZ_BY_MARKET,
)
from app.services.spike_attribution.precompute import MODES, precompute_session
from app.services.spike_attribution.spec import EXPERIMENT_ID, spec_sha256


async def _turnover_top_symbols(
    db: Any, *, market: str, session_date: dt.date, limit: int
) -> list[str]:
    """Universe shortlist by traded value — the issue's '유니버스 거래대금 상위'.

    Uses the most recent session at or before ``session_date`` so a pre-open run
    (before today's bar exists) still ranks on yesterday's turnover.
    """

    table = DAILY_TABLE_BY_MARKET[market]
    tzname = str(SESSION_TZ_BY_MARKET[market])
    stmt = sa.text(
        f"""
        WITH d AS (
            SELECT symbol,
                   (time AT TIME ZONE :tz)::date AS session_date,
                   coalesce(value, close * volume) AS turnover
            FROM {table}
            WHERE (time AT TIME ZONE :tz)::date BETWEEN :lo AND :session_date
        ),
        latest AS (SELECT max(session_date) AS d FROM d)
        SELECT symbol
        FROM d, latest
        WHERE d.session_date = latest.d AND turnover IS NOT NULL
        ORDER BY turnover DESC
        LIMIT :limit
        """  # noqa: S608 - table name comes from a closed literal mapping
    )
    rows = await db.execute(
        stmt,
        {
            "tz": tzname,
            "lo": session_date - dt.timedelta(days=10),
            "session_date": session_date,
            "limit": limit,
        },
    )
    return [row.symbol for row in rows]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    session_date = dt.date.fromisoformat(args.date)
    now = dt.datetime.now(dt.UTC)
    root = Path(args.cache_dir) if args.cache_dir else cache_dir()

    async with AsyncSessionLocal() as db:
        symbols = list(args.symbol or [])
        if args.all:
            symbols.extend(
                await _turnover_top_symbols(
                    db, market=args.market, session_date=session_date, limit=args.limit
                )
            )
        seen: set[str] = set()
        ordered = [s for s in symbols if not (s in seen or seen.add(s))]
        run_result = await precompute_session(
            db,
            market=args.market,
            session_date=session_date,
            symbols=ordered,
            mode=args.mode,
            now=now,
            root=root,
        )

    payload = run_result.as_dict()
    payload.update(
        {
            "experiment_id": EXPERIMENT_ID,
            "spec_sha256": spec_sha256(),
            "cache_dir": str(root),
            "forecast_save_called": False,
        }
    )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="kr", choices=sorted(DAILY_TABLE_BY_MARKET))
    parser.add_argument("--date", required=True, help="session date, YYYY-MM-DD local")
    parser.add_argument(
        "--mode",
        required=True,
        choices=sorted(MODES),
        help=f"{MODE_PREOPEN} (long cadence) or {MODE_INTRADAY} (15 min cadence)",
    )
    parser.add_argument("--symbol", action="append", help="repeatable")
    parser.add_argument(
        "--all", action="store_true", help="add the turnover-ranked universe top N"
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--cache-dir", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.symbol and not args.all:
        print("nothing to do: pass --symbol or --all")
        return 2
    payload = asyncio.run(run(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    # A partial run is not a success. Exit 1 so an operator wrapper cannot read
    # "some symbols failed" as a clean run.
    return 0 if payload["run_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
