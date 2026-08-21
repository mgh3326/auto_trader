#!/usr/bin/env python3
"""Sentry Profiling Collection-Path Canary

obs/sentry-profiling-path: an operator-only, scheduleless CLI to prove (or
disprove) that a deployed process's Sentry profiling collection path
actually reaches Sentry. It does two things, and only two things:

1. Default (no flags): print non-secret diagnostics
   (``app.monitoring.sentry_diagnostics.get_sentry_diagnostics``) and exit.
   No Sentry init, no network call.
2. ``--send --confirm`` (both required, double explicit intent): initialize
   Sentry with the process's real configured DSN, start exactly one fixed,
   synthetic CPU transaction, flush with a bounded timeout, and print a
   sanitized result (event id, whether the SDK reports it as sampled — never
   the DSN, never event payload content).

This script sends no user input, no free text, no MCP prompt/result, no
account or broker data — the transaction name and workload below are fixed
constants. It registers with no scheduler/cron/TaskIQ/Prefect; it only runs
when an operator invokes it directly.

Exit codes:
    0 - diagnostics printed (dry mode), or canary sent successfully
    1 - canary requested but Sentry is not configured (no DSN) — fail closed
    2 - --send/--confirm given without the other (incomplete double intent)

Usage:
    uv run python -m scripts.sentry_profiling_canary
    uv run python -m scripts.sentry_profiling_canary --send --confirm
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.core.config import settings
from app.monitoring.sentry import init_sentry
from app.monitoring.sentry_diagnostics import get_sentry_diagnostics

logger = logging.getLogger(__name__)

PROCESS_KIND = "sentry-profiling-canary"
SERVICE_NAME = "auto-trader-sentry-canary"

# Fixed, non-secret, never derived from user input — this is the whole point
# of a canary: the same probe every time, so a missing profile means the
# collection path is broken, not that this particular payload was unusual.
FIXED_TRANSACTION_NAME = "sentry-profiling-canary"
FIXED_TRANSACTION_OP = "canary.cpu_probe"
CPU_WORKLOAD_ITERATIONS = 2_000_000

DEFAULT_FLUSH_TIMEOUT_SECONDS = 5.0
MAX_FLUSH_TIMEOUT_SECONDS = 15.0


def _run_synthetic_cpu_workload() -> int:
    """Fixed, deterministic CPU work — enough for the profiler to sample."""
    total = 0
    for i in range(CPU_WORKLOAD_ITERATIONS):
        total += i * i
    return total


def _bounded_flush_timeout(requested: float) -> float:
    return max(0.1, min(requested, MAX_FLUSH_TIMEOUT_SECONDS))


def run_dry_diagnostics() -> int:
    """Print non-secret diagnostics only. No init, no network."""
    diagnostics = get_sentry_diagnostics(PROCESS_KIND)
    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    return 0


def run_send_canary(*, flush_timeout_seconds: float) -> int:
    """Actually send one fixed synthetic transaction. Requires a real DSN."""
    diagnostics = get_sentry_diagnostics(PROCESS_KIND)
    if not diagnostics["enabled"]:
        logger.error(
            "Sentry canary requested but SENTRY_DSN is not configured "
            "(process_kind=%s); refusing to fake a success",
            PROCESS_KIND,
        )
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
        return 1

    initialized = init_sentry(service_name=SERVICE_NAME)
    if not initialized:
        logger.error("Sentry init_sentry() returned False; canary send aborted")
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
        return 1

    import sentry_sdk

    with sentry_sdk.start_transaction(
        name=FIXED_TRANSACTION_NAME, op=FIXED_TRANSACTION_OP
    ) as transaction:
        _run_synthetic_cpu_workload()
        event_id = transaction.event_id
        sampled = bool(transaction.sampled)

    sentry_sdk.flush(timeout=_bounded_flush_timeout(flush_timeout_seconds))

    result = {
        **diagnostics,
        "transaction_name": FIXED_TRANSACTION_NAME,
        "event_id": event_id,
        "sampled": sampled,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the canary transaction (requires --confirm too).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Explicit confirmation for --send (requires --send too).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_FLUSH_TIMEOUT_SECONDS,
        help=(
            "Bounded flush timeout in seconds for --send "
            f"(capped at {MAX_FLUSH_TIMEOUT_SECONDS}s)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    args = build_arg_parser().parse_args(argv)

    if args.send != args.confirm:
        logger.error(
            "--send and --confirm must both be given to actually send a "
            "canary transaction (double explicit intent); got --send=%s "
            "--confirm=%s. Falling back to dry diagnostics is not safe here "
            "— failing closed instead.",
            args.send,
            args.confirm,
        )
        return 2

    if args.send and args.confirm:
        return run_send_canary(flush_timeout_seconds=args.timeout)

    return run_dry_diagnostics()


if __name__ == "__main__":
    sys.exit(main())
