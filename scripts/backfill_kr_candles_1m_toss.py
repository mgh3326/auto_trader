#!/usr/bin/env python3
"""#1086 backfill: DART target filings -> Toss 1m bars in research.kr_candles_1m_toss.

Dry-run is the default: it reads ``market_events`` and ``kr_symbol_universe``
(SELECT only), prints the planned symbols and D0/D+1 sessions, and makes no
Toss call and no write. ``--commit`` fetches and writes.

    # plan only (default)
    uv run python -m scripts.backfill_kr_candles_1m_toss \\
        --from-date 2026-07-22 --to-date 2026-09-30

    # collect and write, supply contracts only
    uv run python -m scripts.backfill_kr_candles_1m_toss \\
        --from-date 2026-07-22 --to-date 2026-09-30 \\
        --types SUPPLY_MAND,SUPPLY_VOL --commit --gap-log gaps.jsonl

Idempotent: rows are keyed ``(time_utc, symbol)`` with ``ON CONFLICT DO
NOTHING``, so a re-run inserts nothing new and reports existing rows as
``existing_same`` (or ``existing_conflict`` if Toss now disagrees; the stored
row is kept). Requests whose D+1 has not finished (20:00 KST + 10 min) are
reported ``immature`` and picked up by a later run. With ``--commit`` a
request whose D0 and D+1 already hold KRX_REGULAR bars is skipped as
``already_collected`` (no Toss call) unless ``--refetch`` is given, so a
rolling daily window is cheap to re-run.

Rate: every page goes through ``TossReadClient.minute_candles`` and so through
the shared MARKET_DATA_CHART limiter (``from_settings``). ``--max-tps`` adds a
stricter per-process pace on top of it (default 2/s, never above the group's
configured limit) to leave chart budget for production readers.

Nothing here registers a schedule. Stored ``time_utc`` is the Toss bar END;
research must read ``bar_start = time_utc - interval '1 minute'``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.services.research_candles.dart_minute_trigger import (
    TARGET_COHORTS,
    TriggerPlan,
    load_dart_rows,
    load_symbol_index,
    plan_collection_requests,
)
from app.services.research_candles.toss_minute_collector import (
    BAR_START_SQL,
    TARGET_TABLE,
    CollectionResult,
    JsonlGapSink,
    SqlCandleWriter,
    TossMinuteCollector,
    assert_insert_privilege,
    regular_bar_counts,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TPS = 2.0


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_types(value: str) -> frozenset[str]:
    types = frozenset(t.strip() for t in value.split(",") if t.strip())
    unknown = types - TARGET_COHORTS
    if not types or unknown:
        raise argparse.ArgumentTypeError(
            f"--types must be a comma list from {sorted(TARGET_COHORTS)}"
        )
    return types


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--from-date", type=_parse_date, required=True)
    parser.add_argument("--to-date", type=_parse_date, required=True)
    parser.add_argument(
        "--types",
        type=_parse_types,
        default=TARGET_COHORTS,
        help="comma list of DART cohorts (default: all #1086 targets)",
    )
    parser.add_argument(
        "--commit", action="store_true", help="fetch and write (default: dry run)"
    )
    parser.add_argument(
        "--gap-log",
        type=Path,
        default=None,
        help="JSONL gap log path (required with --commit)",
    )
    parser.add_argument("--max-tps", type=float, default=DEFAULT_MAX_TPS)
    parser.add_argument("--max-pages", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None, help="stop after N requests")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="with --commit: fetch even when D0 and D+1 already hold regular bars",
    )
    args = parser.parse_args(argv)
    if args.to_date < args.from_date:
        parser.error("--to-date must be >= --from-date")
    if args.commit and args.gap_log is None:
        parser.error("--commit requires --gap-log")
    if not 0 < args.max_tps:
        parser.error("--max-tps must be positive")
    if args.max_pages < 1:
        parser.error("--max-pages must be >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    return args


class Pacer:
    """Minimum interval between pages; an extra throttle, never a bypass."""

    def __init__(
        self,
        max_tps: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval = 1.0 / max_tps
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None

    async def __call__(self) -> None:
        now = self.clock()
        if self._last is not None:
            wait = self._last + self.interval - now
            if wait > 0:
                await self.sleep(wait)
                now = self.clock()
        self._last = now


def effective_max_tps(requested: float) -> float:
    from app.services.brokers.toss.rate_limiter import _BASE_LIMITS, TossApiGroup

    return min(requested, float(_BASE_LIMITS[TossApiGroup.MARKET_DATA_CHART]))


def plan_summary(plan: TriggerPlan, *, args: argparse.Namespace) -> dict[str, Any]:
    skipped: dict[str, int] = {}
    for row in plan.skipped:
        skipped[row.reason] = skipped.get(row.reason, 0) + 1
    return {
        "mode": "commit" if args.commit else "dry_run",
        "from_date": args.from_date.isoformat(),
        "to_date": args.to_date.isoformat(),
        "types": sorted(args.types),
        "target_table": TARGET_TABLE,
        "bar_start_sql": BAR_START_SQL,
        "requests": len(plan.requests),
        "symbols": len({r.symbol for r in plan.requests}),
        "cohort_counts": plan.cohort_counts(),
        "skipped": skipped,
    }


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Any],
    client_factory: Callable[[], Any],
    out: Any = sys.stdout,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    async with session_factory() as session:
        rows = await load_dart_rows(session, args.from_date, args.to_date)
        symbol_index = await load_symbol_index(session)
        plan = plan_collection_requests(
            rows, symbol_index=symbol_index, cohorts=args.types
        )
        requests = plan.requests[: args.limit] if args.limit else plan.requests
        print(json.dumps(plan_summary(plan, args=args), ensure_ascii=False), file=out)

        if not args.commit:
            for request in requests:
                print(
                    json.dumps(
                        {
                            "symbol": request.symbol,
                            "d0": request.d0.isoformat(),
                            "d1": request.d1.isoformat(),
                            "cohorts": list(request.cohorts),
                            "rcept_nos": list(request.rcept_nos),
                            "ready": now() >= request.ready_at,
                        },
                        ensure_ascii=False,
                    ),
                    file=out,
                )
            return 0

        await assert_insert_privilege(session)
        run_id = f"{now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        client = client_factory()
        collector = TossMinuteCollector(
            client=client,
            writer=SqlCandleWriter(session),
            gap_sink=JsonlGapSink(args.gap_log),
            run_id=run_id,
            now=now,
            pace=Pacer(effective_max_tps(args.max_tps)),
            max_pages=args.max_pages,
        )
        totals: dict[str, int] = {}
        inserted = 0
        conflicts = 0
        try:
            for request in requests:
                if not args.refetch:
                    stored = await regular_bar_counts(
                        session, request.symbol, request.sessions
                    )
                    if all(stored.get(day, 0) > 0 for day in request.sessions):
                        # Both sessions already hold regular bars: a rolling
                        # re-run costs no Toss call for them.
                        totals["already_collected"] = (
                            totals.get("already_collected", 0) + 1
                        )
                        continue
                try:
                    result: CollectionResult = await collector.collect(request)
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
                totals[result.status] = totals.get(result.status, 0) + 1
                if result.write is not None:
                    inserted += result.write.inserted
                    conflicts += result.write.existing_conflict
                print(json.dumps(result.summary(), ensure_ascii=False), file=out)
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status_counts": totals,
                    "inserted": inserted,
                    "existing_conflict": conflicts,
                    "gap_log": str(args.gap_log),
                }
            ),
            file=out,
        )
        return 0


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from app.core.cli import setup_logging_and_sentry
    from app.core.db import AsyncSessionLocal

    setup_logging_and_sentry(service_name="kr-candles-1m-toss-backfill")

    def client_factory() -> Any:
        from app.services.brokers.toss.client import TossReadClient

        return TossReadClient.from_settings()

    return await run(
        args, session_factory=AsyncSessionLocal, client_factory=client_factory
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
