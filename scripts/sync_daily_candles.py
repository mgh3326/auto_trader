"""Run the daily-candle sync for one market."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from app.jobs.daily_candles import run_daily_candles_sync


async def _run(market: str) -> tuple[dict[str, Any], int]:
    try:
        result = await run_daily_candles_sync(market=market)
    except Exception:
        print("daily candle sync raised an exception", file=sys.stderr)
        return {}, 1
    return result, int(result.get("status") == "failed")


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
