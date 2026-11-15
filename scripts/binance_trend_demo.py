"""Manual, default-disabled TREND-TSMOM-v1 Futures Demo runner. Never scheduled.

Four mutually exclusive modes, each with the triple gate
(``BINANCE_TREND_DEMO_ENABLED`` and ``BINANCE_FUTURES_DEMO_ENABLED`` exactly
``true`` plus a per-call ``--confirm-demo``):

* ``--once`` — one tick.
* ``--loop`` — the same tick every 60 s, foreground, for the whole operator
  session. It holds the lane liveness lock the heartbeat watcher reads and
  refuses to start beside another loop.
* ``--manage-only`` — one tick that may only recover unsent reservations,
  keep uncertain intents blocked and manage held positions. The executor
  returns before signal evaluation or an entry, and refuses ``_enter`` as a
  second barrier; nothing can open a position.
* ``--readiness`` — no-secret, no-HTTP, no-DB report of the gates, the locked
  envelope and the sealed strategy parameters.

The lookback and hard-stop fraction are the sealed CT-5 values
(``constants.SEAL_*``): the CLI owns no other tuning dial. There is no
scheduler, cron, systemd or TaskIQ registration; the operator runs this in a
foreground tmux pane (see ``docs/runbooks/binance-trend-demo.md``).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
import signal
from typing import Any

from app.core.db import AsyncSessionLocal, engine
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.trend.alerting import (
    SEND_TIMEOUT_SECONDS,
    TrendAlerter,
    TrendRunMonitor,
    alert_enabled,
    build_default_channel,
)
from app.services.brokers.binance.trend.constants import (
    IDENTITY,
    LOCKED_LIMITS,
    SEAL_HARD_STOP_FRACTION,
    SEAL_LOOKBACK_DAYS,
)
from app.services.brokers.binance.trend.control import (
    RunnerHeartbeat,
    TickFailure,
    tick_failures,
)
from app.services.brokers.binance.trend.executor import TrendDemoClient, TrendExecutor
from app.services.brokers.binance.trend.state import TrendStateService
from app.services.brokers.binance.trend.strategy import TrendStrategy

ENV_TREND = "BINANCE_TREND_DEMO_ENABLED"
ENV_FUTURES = "BINANCE_FUTURES_DEMO_ENABLED"
ALERT_ENV = "BINANCE_TREND_ALERT_ENABLED"
TICK_SECONDS = 60
PLAYBOOK_DOC = "docs/runbooks/binance-trend-demo.md"


def _guard_cli(args: argparse.Namespace) -> None:
    """Refuse before any network or DB use; every mode passes through here."""
    if not args.confirm_demo:
        raise SystemExit("TREND requires --confirm-demo for every run")
    if os.environ.get(ENV_TREND) != "true":
        raise SystemExit(f"{ENV_TREND} must be true")
    if os.environ.get(ENV_FUTURES) != "true":
        raise SystemExit(f"{ENV_FUTURES} must be true")


def _readiness_report(environ: dict[str, str]) -> dict[str, Any]:
    """Facts only: no client, no DB, no secret value printed."""
    return {
        "event": "trend_readiness",
        "lane": IDENTITY,
        "modes": ["--once", "--loop", "--manage-only", "--readiness"],
        "gates": {
            ENV_TREND: environ.get(ENV_TREND) == "true",
            ENV_FUTURES: environ.get(ENV_FUTURES) == "true",
            ALERT_ENV: environ.get(ALERT_ENV) == "true",
        },
        "universe": list(LOCKED_LIMITS.universe),
        "leg_notional_cap_usdt": format(LOCKED_LIMITS.leg_notional_cap_usdt, "f"),
        "max_concurrent_positions": LOCKED_LIMITS.max_concurrent_positions,
        "max_gross_notional_usdt": format(LOCKED_LIMITS.max_gross_notional_usdt, "f"),
        "max_consecutive_hard_stops_per_utc_day": (
            LOCKED_LIMITS.max_consecutive_hard_stops_per_utc_day
        ),
        "max_conflicts_per_utc_week": LOCKED_LIMITS.max_conflicts_per_utc_week,
        "lookback_days": SEAL_LOOKBACK_DAYS,
        "hard_stop_fraction": format(SEAL_HARD_STOP_FRACTION, "f"),
        "exits": ["hard_stop", "signal_flip", "time_exit"],
        "scheduler": "none",
        "playbook": PLAYBOOK_DOC,
    }


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


def _build_monitor() -> TrendRunMonitor:
    enabled = alert_enabled(os.environ)
    channel = build_default_channel() if enabled else None
    return TrendRunMonitor(TrendAlerter(channel=channel, enabled=enabled))


def _alert_pairs(failures: tuple[TickFailure, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((failure.signature, failure.bucket) for failure in failures)


async def _report_stop(
    monitor: TrendRunMonitor, *, operator: bool, reason: str
) -> None:
    """Send the stop alert even if stop signals keep landing while it is in flight."""
    loop = asyncio.get_running_loop()
    report = asyncio.ensure_future(monitor.stopped(operator=operator, reason=reason))
    deadline = loop.time() + SEND_TIMEOUT_SECONDS + 1
    interrupted = False
    while not report.done() and loop.time() < deadline:
        try:
            await asyncio.wait({report}, timeout=max(deadline - loop.time(), 0))
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


def _payload(result: Any) -> dict[str, Any]:
    return dataclasses.asdict(result)


async def _run_ticks(
    args: argparse.Namespace,
    *,
    client: TrendDemoClient,
    strategy: TrendStrategy,
    state: TrendStateService,
    monitor: TrendRunMonitor,
    stop: _StopState,
    heartbeat: RunnerHeartbeat | None,
) -> int:
    once = args.once or args.manage_only
    try:
        while True:
            async with AsyncSessionLocal() as db:
                executor = TrendExecutor(
                    client=client,
                    strategy=strategy,
                    state=state,
                    demo_ledger=BinanceDemoLedgerService(db),
                    ledger_session=db,
                )
                try:
                    result = await executor.run_tick(
                        now=dt.datetime.now(dt.UTC),
                        confirm=True,
                        manage_only=args.manage_only,
                    )
                    payload: dict[str, Any] = _payload(result)
                    failures = tick_failures(result.events)
                except Exception as exc:
                    payload = {"event": "blocked", "error_class": type(exc).__name__}
                    failures = (
                        TickFailure(f"blocked:{type(exc).__name__}", "blocked"),
                    )
                print(json.dumps(payload, sort_keys=True), flush=True)
            # The heartbeat is independent of alerting: the watcher reads it
            # whether or not this process can send alerts. The stamp both
            # refreshes it and proves this backend still holds the lane lock; a
            # lost lock means another runner owns the lane, so this loop stops
            # instead of trading beside it.
            if heartbeat is not None and not await heartbeat.stamp():
                raise RuntimeError("trend runner liveness lock lost")
            await monitor.tick_done(_alert_pairs(failures))
            if once:
                # Inside the try: a stop that lands while the alert is still
                # being delivered goes through the handlers below, never around them.
                await monitor.drain()
                return 2 if failures else 0
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        if not monitor.enabled:
            raise
        await _report_stop(monitor, operator=stop.operator, reason=stop.reason)
        if not stop.installed:
            raise
        return 130 if stop.operator else 143
    except KeyboardInterrupt:
        await monitor.stopped(operator=True, reason="keyboard_interrupt")
        raise
    except Exception as exc:
        if monitor.enabled:
            await _report_stop(
                monitor, operator=False, reason=f"exception:{type(exc).__name__}"
            )
            await monitor.drain()
        raise


async def _run(args: argparse.Namespace) -> int:
    _guard_cli(args)
    if args.readiness:
        print(
            json.dumps(_readiness_report(dict(os.environ)), sort_keys=True), flush=True
        )
        return 0
    client = TrendDemoClient.from_env()
    strategy = TrendStrategy(
        lookback_days=SEAL_LOOKBACK_DAYS,
        hard_stop_fraction=SEAL_HARD_STOP_FRACTION,
    )
    state = TrendStateService(AsyncSessionLocal)
    monitor = _build_monitor()
    stop = _StopState()
    heartbeat = RunnerHeartbeat(engine) if args.loop else None
    try:
        if monitor.enabled:
            _install_stop_signals(stop)
        if heartbeat is not None:
            if not await heartbeat.open():
                raise SystemExit("another TREND runner owns the loop lock")
            if not await heartbeat.stamp():
                raise SystemExit("trend runner liveness lock lost")
        return await _run_ticks(
            args,
            client=client,
            strategy=strategy,
            state=state,
            monitor=monitor,
            stop=stop,
            heartbeat=heartbeat,
        )
    finally:
        if heartbeat is not None:
            await heartbeat.close()
        await client.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "TREND-TSMOM-v1 Futures Demo runner (manual, default disabled). "
            "Modes are mutually exclusive: --once / --loop / --manage-only / "
            "--readiness."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run one tick and exit.")
    mode.add_argument(
        "--loop",
        action="store_true",
        help=(
            f"Run a tick every {TICK_SECONDS}s in the foreground. No scheduler; "
            "stop it with Ctrl-C."
        ),
    )
    mode.add_argument(
        "--manage-only",
        dest="manage_only",
        action="store_true",
        help=(
            "One tick that only recovers reservations, keeps uncertain intents "
            "blocked and manages held positions. It never opens an entry."
        ),
    )
    mode.add_argument(
        "--readiness",
        action="store_true",
        help="No-secret gate/envelope report. No HTTP, no DB, no credentials.",
    )
    parser.add_argument(
        "--confirm-demo",
        action="store_true",
        help="Required on every run; asserts the Futures Demo account and flags.",
    )
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
