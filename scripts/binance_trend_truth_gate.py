"""Read-only TREND-TSMOM-v1 account truth gate. Run before the first runner start.

Reads the Futures Demo account (signed GETs only) and the trend state tables
(SELECT only), then prints one JSON verdict. Exit 0 = PASS, 2 = FAIL. It never
sends an order, changes leverage or margin, or writes any row. The gate has no
Binance write path; ``test_truth_gate.py`` pins that with an AST test.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.trend.executor import TrendDemoClient
from app.services.brokers.binance.trend.state import TrendStateService
from app.services.brokers.binance.trend.truth_gate import run_truth_gate


def _guard_cli(args: argparse.Namespace) -> None:
    if not args.confirm_demo:
        raise SystemExit("TREND truth gate requires --confirm-demo for every run")
    if os.environ.get("BINANCE_TREND_DEMO_ENABLED") != "true":
        raise SystemExit("BINANCE_TREND_DEMO_ENABLED must be true")
    if os.environ.get("BINANCE_FUTURES_DEMO_ENABLED") != "true":
        raise SystemExit("BINANCE_FUTURES_DEMO_ENABLED must be true")


async def _run(args: argparse.Namespace) -> int:
    _guard_cli(args)
    client = TrendDemoClient.from_env()
    try:
        report = await run_truth_gate(
            client=client, state=TrendStateService(AsyncSessionLocal)
        )
    finally:
        await client.aclose()
    print(
        json.dumps(
            {
                "event": "trend_truth_gate",
                "verdict": report.verdict,
                "checked_at": dt.datetime.now(dt.UTC).isoformat(),
                "checks": [dataclasses.asdict(check) for check in report.checks],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if report.verdict == "PASS" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only TREND account truth gate")
    parser.add_argument("--confirm-demo", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
