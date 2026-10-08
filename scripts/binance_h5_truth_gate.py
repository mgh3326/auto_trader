"""Read-only H5 account truth gate. Run before the first runner start.

Reads the Futures Demo account (signed GETs only), the H5 state tables and the
shared Binance Demo ledger, then prints one JSON verdict. Exit 0 = PASS, 2 =
FAIL. It never sends an order, changes leverage or margin, or writes any row.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.h5.client import H5DemoClient
from app.services.brokers.binance.h5.state import H5StateService
from app.services.brokers.binance.h5.truth_gate import run_truth_gate


def _guard_cli(args: argparse.Namespace) -> None:
    if not args.confirm_demo:
        raise SystemExit("H5 truth gate requires --confirm-demo for every run")
    if os.environ.get("BINANCE_H5_DEMO_ENABLED") != "true":
        raise SystemExit("BINANCE_H5_DEMO_ENABLED must be true")
    if os.environ.get("BINANCE_FUTURES_DEMO_ENABLED") != "true":
        raise SystemExit("BINANCE_FUTURES_DEMO_ENABLED must be true")


async def _run(args: argparse.Namespace) -> int:
    _guard_cli(args)
    client = H5DemoClient.from_env()
    try:
        async with AsyncSessionLocal() as db:
            report = await run_truth_gate(
                client=client,
                state=H5StateService(AsyncSessionLocal),
                ledger=BinanceDemoLedgerService(db),
            )
    finally:
        await client.aclose()
    print(
        json.dumps(
            {
                "event": "h5_truth_gate",
                "verdict": report.verdict,
                "checked_at": dt.datetime.now(dt.UTC).isoformat(),
                "checks": [dataclasses.asdict(check) for check in report.checks],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if report.verdict == "PASS" else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only H5 account truth gate")
    parser.add_argument("--confirm-demo", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
