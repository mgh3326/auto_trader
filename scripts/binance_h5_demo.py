"""Manual, default-disabled H5 Futures Demo runner. Never scheduled."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.h5.client import H5DemoClient
from app.services.brokers.binance.h5.executor import H5Executor
from app.services.brokers.binance.h5.state import H5StateService
from app.services.brokers.binance.h5.strategy import H5Strategy


def _guard_cli(args: argparse.Namespace) -> None:
    if not args.confirm_demo:
        raise SystemExit("H5 requires --confirm-demo for every run")
    if os.environ.get("BINANCE_H5_DEMO_ENABLED") != "true":
        raise SystemExit("BINANCE_H5_DEMO_ENABLED must be true")
    if os.environ.get("BINANCE_FUTURES_DEMO_ENABLED") != "true":
        raise SystemExit("BINANCE_FUTURES_DEMO_ENABLED must be true")


async def _run(args: argparse.Namespace) -> int:
    _guard_cli(args)
    client = H5DemoClient.from_env()
    strategy = H5Strategy(base_url=client._base_url)
    state = H5StateService(AsyncSessionLocal)
    try:
        while True:
            async with AsyncSessionLocal() as db:
                executor = H5Executor(
                    client=client,
                    strategy=strategy,
                    state=state,
                    demo_ledger=BinanceDemoLedgerService(db),
                    ledger_session=db,
                )
                try:
                    result = await executor.run_tick(
                        now=dt.datetime.now(dt.UTC), confirm=True
                    )
                    payload: dict[str, Any] = dataclasses.asdict(result)
                except Exception as exc:
                    payload = {"event": "blocked", "error_class": type(exc).__name__}
                print(json.dumps(payload, sort_keys=True), flush=True)
            if not args.loop:
                return (
                    2
                    if payload["event"]
                    in {"blocked", "entry_uncertain", "close_uncertain"}
                    else 0
                )
            await asyncio.sleep(60)
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual H5 Futures Demo runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--confirm-demo", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
