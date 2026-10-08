"""Manual, default-disabled H5 heartbeat watcher. Read-only; never scheduled.

The H5 runner stamps ``review.binance_h5_lane_state.updated_at`` at the start of
every tick. This watcher reads that stamp (SELECT only, no broker call, no
credentials needed) and alerts once per episode when it is older than
``--miss-minutes``. It is the only alert path that survives a SIGKILL, an OOM
kill or a lost host. Start it after the runner; stop it before the runner.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.h5.alerting import (
    DEFAULT_MISS_MINUTES,
    MIN_MISS_MINUTES,
    AlertKind,
    H5Alerter,
    alert_enabled,
    build_default_channel,
    poll_heartbeat,
)
from app.services.brokers.binance.h5.state import H5StateService

MIN_POLL_SECONDS = 10


def _guard(args: argparse.Namespace) -> None:
    if not alert_enabled(os.environ):
        raise SystemExit("BINANCE_H5_ALERT_ENABLED must be true")


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
    state: Any,
    alerter: H5Alerter,
    clock: Callable[[], dt.datetime] = _utcnow,
) -> int:
    miss_after = dt.timedelta(minutes=args.miss_minutes)
    while True:
        record = await poll_heartbeat(
            state, alerter, miss_after=miss_after, now=clock()
        )
        print(json.dumps(record, sort_keys=True), flush=True)
        if args.once:
            return 2 if record["verdict"] in {"missed", "unreadable"} else 0
        await asyncio.sleep(args.poll_seconds)


async def _send_test(alerter: H5Alerter) -> int:
    delivered = await alerter.fire(AlertKind.TEST, "operator_send_test")
    print(
        json.dumps({"event": "alert_test", "delivered": delivered}, sort_keys=True),
        flush=True,
    )
    return 0 if delivered else 2


async def _run(args: argparse.Namespace) -> int:
    _guard(args)
    alerter = H5Alerter(channel=build_default_channel(), enabled=True)
    if args.send_test:
        return await _send_test(alerter)
    return await _watch(args, state=H5StateService(AsyncSessionLocal), alerter=alerter)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual H5 heartbeat watcher")
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
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print(json.dumps({"event": "watch_stopped", "by": "operator"}), file=sys.stderr)
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
