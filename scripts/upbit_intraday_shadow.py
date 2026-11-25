#!/usr/bin/env python3
"""hk 6036: manual Upbit KRW intraday shadow recorder (no orders).

    uv run python -m scripts.upbit_intraday_shadow [--commit]
        [--catchup-bars N] [--markets KRW-BTC,KRW-XRP]

Reads Upbit's public market list and closed 15-minute candles (no key), evaluates
the frozen ``upbit-intraday-v1`` rules on the newest closed bars, and resolves
virtual round trips whose bars exist.

The default is a dry run: no database access, candidates and virtual rows are
printed. ``--commit`` inserts into the two ``research.upbit_shadow_*`` tables
(insert-only). Nothing places, previews or books an order. Not scheduled by this
repository.

Output: one JSON object. Exit 0 = clean, 1 = at least one market errored or the
run failed, 2 = refused (usage, or the frozen rules file changed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.cli import setup_logging_and_sentry
from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow.public_data import UpbitPublicData
from app.services.upbit_intraday_shadow.recorder import run_once


def _catchup(value: str) -> int:
    bars = int(value)
    if not 1 <= bars <= K.MAX_CATCHUP_BARS:
        raise argparse.ArgumentTypeError(f"must be 1..{K.MAX_CATCHUP_BARS}")
    return bars


def _markets(value: str) -> list[str]:
    markets = [m.strip().upper() for m in value.split(",") if m.strip()]
    if not markets or any(not m.startswith("KRW-") for m in markets):
        raise argparse.ArgumentTypeError("expects KRW-* markets, comma separated")
    return markets


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--catchup-bars", type=_catchup, default=K.DEFAULT_CATCHUP_BARS)
    parser.add_argument("--markets", type=_markets, default=None)
    return parser.parse_args(argv)


async def run(
    args: argparse.Namespace, data: UpbitPublicData | None = None
) -> dict[str, Any]:
    data = data or UpbitPublicData()
    run_id = f"upbit-shadow-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    options = {
        "run_id": run_id,
        "catchup_bars": args.catchup_bars,
        "markets": args.markets,
    }
    if not args.commit:
        return await run_once(data, session=None, **options)
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await run_once(data, session=session, **options)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging_and_sentry(service_name="upbit-intraday-shadow")
    try:
        report = asyncio.run(run(args))
    except K.FrozenRulesChanged as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    print(json.dumps(report, sort_keys=True, default=str, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
