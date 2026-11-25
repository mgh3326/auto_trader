#!/usr/bin/env python3
"""hk 6145: manual collector — closed KIS 1-minute bars -> research minute tables.

    uv run python -m scripts.collect_session_minutes --market kr|us \\
        [--symbols 005930,000660] [--dry-run]

KR reads KIS market J (venue KRX) and NX (venue NTX, nxt_eligible symbols only)
into ``research.kr_candles_1m``; US reads the overseas minute chart into
``research.us_candles_1m``. Only closed bars are written, insert-only
(``ON CONFLICT DO NOTHING``), so ``retrieved_at`` is the first receipt. Nothing
is written to ``public.*_candles_1m``. Not scheduled by this repository.

Targets (default), in tier order (hk 6299): (1) fixed — KIS + manual holdings and
the fixed list (``SESSION_MINUTES_FIXED_SYMBOLS_{KR,US}``, default benchmarks 069500,
229200 / SPY,QQQ,SMH), never cut, budget ``SESSION_MINUTES_BUDGET_FIXED`` (50);
(2) KR only — today's first-capture intraday movers (hk 6154), ordered by earliest
capture then best rank, budget ``SESSION_MINUTES_BUDGET_INTRADAY`` (40); (3) the
top ``SESSION_MINUTES_SCREENER_TOP_N`` (default 20) of every available screener
preset (DB-only, no pick-log write), budget ``SESSION_MINUTES_BUDGET_SCREENER`` (60);
(4) today's admitted ``review.screener_pick_log`` symbols, the remainder. Unused budget
spills to the next tier. The union is capped at ``SESSION_MINUTES_MAX_SYMBOLS_{KR,US}``
(default 150). ``--symbols`` replaces the union for a targeted run. KR movers with no
minute row yet today get one extra, earlier KIS page (hour cursor).

US day market (hk 6322): for ``--market us`` with ``TOSS_API_ENABLED`` the run also
reads Toss 1m candles for the same targets (source ``TOSS``, venue
``TOSS_INTEGRATED``). The Toss US market calendar is fetched once per run; bars inside
its dayMarket window are ``US_DAY``. KIS US reads are skipped while a day-market window
is open. Toss off: no Toss read, and the report says ``toss: disabled``.

Output: one JSON object (inserted / already_present / skipped_forming / errors) plus
``targeted_symbols``, ``targets_tiers`` (per-tier budget, allowance, candidates,
selected, cut) and ``targets_cuts`` (symbol, tier, reason ``budget_cut``). Feed this
line to ``scripts/diagnose_session_coverage.py --collect-report``.
Exit 0 = clean, 1 = at least one error, 2 = usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from app.core.cli import setup_logging_and_sentry
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import today_kst
from app.services.brokers.kis.client import KISClient
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.market_calendar import get_toss_market_calendar
from app.services.research_candles import session_minutes_collector as collector

if TYPE_CHECKING:
    from app.services.brokers.toss.market_calendar import TossMarketCalendar


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--market", required=True, choices=("kr", "us"))
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="comma and/or space separated; replaces the automatic target union",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-id", type=int, default=1)
    return parser.parse_args(argv)


async def _toss_us_inputs(
    market: str,
) -> tuple[TossReadClient | None, TossMarketCalendar | None, str]:
    """Toss reader and the run's US calendar (hk 6322). Read-only.

    Returns ``(reader, calendar, status)``. The calendar is fetched once per run.
    A failed calendar read leaves the calendar ``None``, so no Toss bar is read and
    the collector reports ``CalendarUnavailable``.
    """
    if market != "us":
        return None, None, "not_requested"
    if not settings.toss_api_enabled:
        return None, None, "disabled"
    calendar = await get_toss_market_calendar("us", today_kst())
    try:
        reader = TossReadClient.from_settings()
    except Exception as exc:  # noqa: BLE001 - reported, the KIS pass still runs
        logging.getLogger(__name__).warning(
            "toss reader unavailable error=%s", type(exc).__name__
        )
        return None, calendar, "reader_unavailable"
    return reader, calendar, "ready"


async def run(args: argparse.Namespace) -> dict[str, object]:
    market = cast(collector.Market, args.market)
    now = datetime.now(UTC)
    kis = cast(collector.KisMinuteReader, KISClient())
    toss, us_calendar, toss_input_status = await _toss_us_inputs(market)
    fixed_raw = (
        settings.SESSION_MINUTES_FIXED_SYMBOLS_KR
        if market == "kr"
        else settings.SESSION_MINUTES_FIXED_SYMBOLS_US
    )
    max_symbols = (
        settings.SESSION_MINUTES_MAX_SYMBOLS_KR
        if market == "kr"
        else settings.SESSION_MINUTES_MAX_SYMBOLS_US
    )
    budgets = {
        "fixed_budget": settings.SESSION_MINUTES_BUDGET_FIXED,
        "intraday_budget": settings.SESSION_MINUTES_BUDGET_INTRADAY,
        "screener_budget": settings.SESSION_MINUTES_BUDGET_SCREENER,
    }
    async with AsyncSessionLocal() as session:
        if args.symbols:
            symbols = collector.parse_symbol_list(market, ",".join(args.symbols))
            targets = collector.TargetSet(
                symbols=symbols, by_origin={"cli": len(symbols)}
            )
        else:
            targets = await collector.build_targets(
                market=market,
                session=session,
                kis=kis,
                user_id=args.user_id,
                fixed_symbols_raw=fixed_raw,
                now=now,
                screener_top_n=settings.SESSION_MINUTES_SCREENER_TOP_N,
                max_symbols=max_symbols,
                **budgets,
            )
        nxt: set[str] = set()
        exchanges: dict[str, str] = {}
        unresolved: list[str] = []
        if market == "kr":
            nxt = await collector.load_nxt_eligible(session, targets.symbols)
        else:
            exchanges, unresolved = await collector.resolve_us_exchanges(
                session, targets.symbols
            )
        try:
            report = await collector.collect(
                market=market,
                symbols=targets.symbols,
                kis=kis,
                writer=None
                if args.dry_run
                else collector.SqlMinuteWriter(session, market),
                nxt_eligible=nxt,
                us_exchanges=exchanges,
                unresolved_symbols=unresolved,
                dry_run=args.dry_run,
                prior_errors=targets.errors,
                backfill_symbols=targets.backfill,
                toss=toss,
                us_calendar=us_calendar,
            )
        finally:
            if toss is not None:
                await toss.aclose()
    if toss is None and toss_input_status != "not_requested":
        report["toss"] = toss_input_status
    report.update(collector.targets_report(targets))
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="collect-session-minutes")
    report = asyncio.run(run(args))
    print(json.dumps(report, sort_keys=True, default=str))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
