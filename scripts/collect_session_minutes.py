#!/usr/bin/env python3
"""hk 6145: manual collector — closed KIS 1-minute bars -> research minute tables.

    uv run python -m scripts.collect_session_minutes --market kr|us \\
        [--symbols 005930,000660] [--dry-run]

KR reads KIS market J (venue KRX) and NX (venue NTX, nxt_eligible symbols only)
into ``research.kr_candles_1m``; US reads the overseas minute chart into
``research.us_candles_1m``. Only closed bars are written, insert-only
(``ON CONFLICT DO NOTHING``), so ``retrieved_at`` is the first receipt. Nothing
is written to ``public.*_candles_1m``. Not scheduled by this repository.

Targets (default): KIS holdings + manual holdings + today's admitted
``review.screener_pick_log`` symbols + ``SESSION_MINUTES_FIXED_SYMBOLS_{KR,US}``.
``--symbols`` replaces that union for a targeted run.

Output: one JSON object (inserted / already_present / skipped_forming / errors).
Exit 0 = clean, 1 = at least one error, 2 = usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import cast

from app.core.cli import setup_logging_and_sentry
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.research_candles import session_minutes_collector as collector


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


async def run(args: argparse.Namespace) -> dict[str, object]:
    market = cast(collector.Market, args.market)
    now = datetime.now(UTC)
    kis = cast(collector.KisMinuteReader, KISClient())
    fixed_raw = (
        settings.SESSION_MINUTES_FIXED_SYMBOLS_KR
        if market == "kr"
        else settings.SESSION_MINUTES_FIXED_SYMBOLS_US
    )
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
        report = await collector.collect(
            market=market,
            symbols=targets.symbols,
            kis=kis,
            writer=None if args.dry_run else collector.SqlMinuteWriter(session, market),
            nxt_eligible=nxt,
            us_exchanges=exchanges,
            unresolved_symbols=unresolved,
            dry_run=args.dry_run,
            prior_errors=targets.errors,
        )
    report["targets_by_origin"] = targets.by_origin
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="collect-session-minutes")
    report = asyncio.run(run(args))
    print(json.dumps(report, sort_keys=True, default=str))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
