#!/usr/bin/env python3
"""hk 6148: manual collector — closed KIS futures 1-minute bars -> research table.

    KIS_FUTURES_QUOTES_ENABLED=true \\
        uv run python -m scripts.collect_futures_minutes [--dry-run]

Reads the front and next contract of KOSPI200 futures and mini KOSPI200 futures
(resolved from the KIS futures master) through the read-only quotes view
(``app.services.kis_futures_quotes.view``, live KIS host, no account number) and
writes closed day-session bars insert-only (``ON CONFLICT DO NOTHING``) into
``research.kr_futures_candles_1m``. The night session is reported as
``night: "unsupported"`` — the official KIS samples document no night quote TR.
Nothing touches orders, ledgers or ``public.*`` tables. Not scheduled by this
repository (no Dagu/cron registration).

Gate: ``KIS_FUTURES_QUOTES_ENABLED`` exactly ``true`` (default off). Without it the
run stops before any token lookup, HTTP or DB write.

Output: one JSON object (inserted / already_present / skipped_forming / errors /
products_resolved / night). Exit 0 = clean, 1 = at least one error, 2 = gate off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, cast

from app.core.cli import setup_logging_and_sentry
from app.services.kis_futures_quotes.view import FuturesQuotesGateOff, open_quotes_view
from app.services.research_candles import futures_minutes_collector as collector


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    view = open_quotes_view()  # raises before anything else when the gate is off
    try:
        if args.dry_run:
            return await collector.collect(
                view=cast(collector.QuotesReader, view), writer=None, dry_run=True
            )
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            return await collector.collect(
                view=cast(collector.QuotesReader, view),
                writer=collector.SqlFuturesMinuteWriter(session),
                dry_run=False,
            )
    finally:
        await view.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="collect-futures-minutes")
    try:
        report = asyncio.run(run(args))
    except FuturesQuotesGateOff as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, sort_keys=True, default=str))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
