"""Manual, default-disabled H5 Futures Demo runner. Never scheduled."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
import signal
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.h5.alerting import (
    FAILURE_EVENTS,
    H5Alerter,
    H5RunMonitor,
    alert_enabled,
    build_default_channel,
)
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


class _StopState:
    """Why the loop was cancelled; only filled in when alerts are enabled."""

    def __init__(self) -> None:
        self.installed = False
        self.operator = False
        self.reason = "cancelled"


def _install_stop_signals(stop: _StopState) -> None:
    """Ctrl-C is the operator's own stop; SIGTERM and anything else is not."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is None:
        return

    def _on_signal(reason: str, operator: bool) -> None:
        stop.reason, stop.operator = reason, operator
        task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _on_signal, "sigint", True)
        loop.add_signal_handler(signal.SIGTERM, _on_signal, "sigterm", False)
    except (NotImplementedError, RuntimeError):
        return
    stop.installed = True


def _build_monitor() -> H5RunMonitor:
    enabled = alert_enabled(os.environ)
    channel = build_default_channel() if enabled else None
    return H5RunMonitor(H5Alerter(channel=channel, enabled=enabled))


async def _run_ticks(
    args: argparse.Namespace,
    *,
    client: H5DemoClient,
    strategy: H5Strategy,
    state: H5StateService,
    monitor: H5RunMonitor,
    stop: _StopState,
) -> int:
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
            await monitor.tick_done(payload)
            if not args.loop:
                code = 2 if payload["event"] in FAILURE_EVENTS else 0
                # Inside the try: a stop that lands while the alert is still
                # being delivered goes through the handlers below, never around them.
                await monitor.drain()
                return code
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        if not monitor.enabled:
            raise
        await monitor.stopped(operator=stop.operator, reason=stop.reason)
        if not stop.installed:
            raise
        return 130 if stop.operator else 143
    except KeyboardInterrupt:
        await monitor.stopped(operator=True, reason="keyboard_interrupt")
        raise
    except Exception as exc:
        await monitor.stopped(operator=False, reason=f"exception:{type(exc).__name__}")
        await monitor.drain()
        raise


async def _run(args: argparse.Namespace) -> int:
    _guard_cli(args)
    client = H5DemoClient.from_env()
    strategy = H5Strategy(base_url=client._base_url)
    state = H5StateService(AsyncSessionLocal)
    monitor = _build_monitor()
    stop = _StopState()
    try:
        if monitor.enabled:
            _install_stop_signals(stop)
        return await _run_ticks(
            args,
            client=client,
            strategy=strategy,
            state=state,
            monitor=monitor,
            stop=stop,
        )
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
