#!/usr/bin/env python3
"""KR-B0 KIS mock runner operator entry point.

This is a scheduleless, foreground-only process.  It remains disabled unless
``KIS_MOCK_RUNNER_ENABLED=true`` is explicitly present, and B0 has no strategy
overlay, so its normal ``--once`` result is ``OVERLAY_REQUIRED`` with zero
broker calls.  No option accepts a symbol, strategy, pricing rule, or safety
envelope override.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from app.services.kis_mock_runner.control import (
    KillSwitchRearmUnauthorized,
    PostgresKillSwitchStore,
    rearm_active,
)
from app.services.kis_mock_runner.gates import (
    KISMockRunnerRearmUnauthorized,
    assert_rearm_authorized,
)
from app.services.kis_mock_runner.runner import KISMockRunner, RunnerResult

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "KIS mock runner (KR-B0): default-disabled, foreground supervised, "
            "strategy-neutral."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run one guarded tick.")
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run the fixed-cadence foreground loop; no scheduler is registered.",
    )
    mode.add_argument(
        "--rearm",
        action="store_true",
        help="Operator-only durable kill-switch re-arm; also needs --confirm.",
    )
    parser.add_argument(
        "--tag",
        default="foreground",
        help="Correlation tag only; it does not select a strategy or symbol.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required per-invocation confirmation for --rearm.",
    )
    parser.add_argument(
        "--updated-by",
        default="",
        help="Required non-secret operator audit label for --rearm.",
    )
    return parser.parse_args(argv)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _result_payload(result: RunnerResult) -> dict[str, Any]:
    return {
        "event": "kis_mock_runner",
        "status": result.status.value,
        "kill_mode": None
        if result.kill_switch is None
        else result.kill_switch.mode.value,
        "kill_reason": None
        if result.kill_switch is None
        else result.kill_switch.reason,
        "canceled_pending_entries": result.canceled_pending_entries,
        "correlation_id": result.correlation_id,
        "broker_calls": result.broker_calls,
        "notification_delivery": [
            {
                "channel": report.channel,
                "delivered": report.delivered,
                "skipped": report.skipped,
                "retry_recorded": report.retry_recorded,
            }
            for report in result.notification_reports
        ],
    }


async def _run(args: argparse.Namespace, *, environment: dict[str, str]) -> int:
    if args.rearm:
        try:
            assert_rearm_authorized(environment, confirm=args.confirm)
            state = await rearm_active(
                PostgresKillSwitchStore(),
                operator_gate=True,
                confirm=args.confirm,
                updated_by=args.updated_by,
            )
        except (KISMockRunnerRearmUnauthorized, KillSwitchRearmUnauthorized) as exc:
            _emit({"event": "kis_mock_runner_rearm_refused", "reason": str(exc)})
            return 2
        _emit(
            {
                "event": "kis_mock_runner_rearmed",
                "mode": state.mode.value,
                "updated_by": state.updated_by,
            }
        )
        return 0

    runner = KISMockRunner(environment=environment, tag=args.tag)
    result = await (runner.run_forever() if args.loop else runner.run_once())
    _emit(_result_payload(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args, environment=dict(os.environ)))
    except Exception as exc:  # noqa: BLE001 - fail closed, no secrets in output
        logger.exception(
            "KIS mock runner stopped before mutation: %s", type(exc).__name__
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
