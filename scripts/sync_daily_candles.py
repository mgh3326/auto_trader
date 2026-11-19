"""Run the daily-candle sync for one market.

Exit codes: 0 = every target synced; 3 = partial result (some symbols synced,
``failed_count`` > 0: attention); 1 = job failed (every target failed, or the
job raised before or outside the per-symbol loop, for example the database
was unreachable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from app.jobs.daily_candles import run_daily_candles_sync

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_ATTENTION = 3


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("status") == "ok":
        return EXIT_OK
    # ``status == "failed"`` is the job's own verdict when every target failed
    # (nothing was synced), so it stays a job failure; only a partial loop is
    # attention.
    failed_count = result.get("failed_count")
    if result.get("status") == "partial" and isinstance(failed_count, int):
        if failed_count > 0:
            return EXIT_ATTENTION
    return EXIT_JOB_FAILED


async def _run(market: str) -> tuple[dict[str, Any], int]:
    try:
        result = await run_daily_candles_sync(market=market)
    except Exception:
        print("daily candle sync raised an exception", file=sys.stderr)
        return {}, EXIT_JOB_FAILED
    return result, _exit_code(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("kr", "us", "crypto"), required=True)
    args = parser.parse_args(argv)

    result, exit_code = asyncio.run(_run(args.market))
    if result:
        print(json.dumps(result, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
