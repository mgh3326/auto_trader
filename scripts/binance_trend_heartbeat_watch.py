"""Manual, default-disabled TREND heartbeat watcher. Read-only; never scheduled.

The ``--loop`` runner holds a session advisory lock and touches that connection
after every tick. This watcher reads the lock and its backend's
``pg_stat_activity.state_change`` (SELECT only, no broker call, no Binance
credential) and alerts once per episode when the runner is absent or its last
touch is older than ``--miss-minutes``. It is the only alert path that survives
a SIGKILL, an OOM kill or a lost host.

Absence is a failure here (unlike H5, which keeps a durable tick stamp): the
lock is the only liveness evidence this lane has. Start the watcher after the
runner's first tick line, stop it before the runner, as the runbook says.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from collections.abc import Callable

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.trend.alerting import (
    DEFAULT_MISS_MINUTES,
    MIN_MISS_MINUTES,
    AlertKind,
    TrendAlerter,
    alert_enabled,
    build_default_channel,
    poll_heartbeat,
)
from app.services.brokers.binance.trend.control import RunnerLiveness

MIN_POLL_SECONDS = 10


def _guard(args: argparse.Namespace) -> None:
    if not alert_enabled(os.environ):
        raise SystemExit("BINANCE_TREND_ALERT_ENABLED must be true")


def _bounded_int(minimum: int):
    def parse(raw: str) -> int:
        value = int(raw)
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum}")
        return value

    return parse


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _watch(
    args: argparse.Namespace,
    *,
    lane: RunnerLiveness,
    alerter: TrendAlerter,
    clock: Callable[[], dt.datetime] = _utcnow,
) -> int:
    miss_after = dt.timedelta(minutes=args.miss_minutes)
    seen = False
    while True:
        record = await poll_heartbeat(
            lane,
            alerter,
            miss_after=miss_after,
            now=clock(),
            seen_before=seen,
        )
        seen = bool(record["runner_seen"])
        print(json.dumps(record, sort_keys=True), flush=True)
        if args.once:
            bad = record["verdict"] in {"missed", "unreadable"} or (
                record["verdict"] == "absent" and record["runner_seen"]
            )
            return 2 if bad else 0
        await asyncio.sleep(args.poll_seconds)


async def _send_test(alerter: TrendAlerter) -> int:
    delivered = await alerter.fire(AlertKind.TEST, "operator_send_test")
    print(
        json.dumps({"event": "alert_test", "delivered": delivered}, sort_keys=True),
        flush=True,
    )
    return 0 if delivered else 2


async def _run(args: argparse.Namespace) -> int:
    _guard(args)
    alerter = TrendAlerter(channel=build_default_channel(), enabled=True)
    if args.send_test:
        return await _send_test(alerter)
    return await _watch(args, lane=RunnerLiveness(AsyncSessionLocal), alerter=alerter)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual TREND heartbeat watcher")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--send-test", action="store_true")
    parser.add_argument(
        "--miss-minutes",
        type=_bounded_int(MIN_MISS_MINUTES),
        default=DEFAULT_MISS_MINUTES,
    )
    parser.add_argument(
        "--poll-seconds", type=_bounded_int(MIN_POLL_SECONDS), default=60
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print(json.dumps({"event": "watch_stopped", "by": "operator"}), file=sys.stderr)
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
