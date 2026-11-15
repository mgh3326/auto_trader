#!/usr/bin/env python3
"""hk 4722 / hk 4734 (3797 T-P3, T-P4) / hk 4754 — operator CLI for Kiwoom mock reconcile.

Previews verdicts by default and writes nothing. ``--apply-terminal-only``
closes only expired / cancelled / rejected rows and resolves ``uncertain`` to
``accepted``; fills are deferred. ``--apply`` (full mode, KR only) also books
the broker-evidence fill delta into ``execution_ledger`` and journals it with
``account='kiwoom_mock'`` once per order, at its terminal fill. Both need their
exact-true gates. There is no scheduler — this lever is manual (hard rule 6).

``--market`` selects the account: ``kr`` (``kiwoom_mock``, gated by
``KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED``) or ``us`` (``kiwoom_mock_us``,
gated by ``KIWOOM_MOCK_US_TERMINAL_AUTO_RECONCILE_ENABLED``). The two scans never
mix. Full-mode fill booking is KR only (T-P4); ``--apply`` with ``--market us``
is refused.

Prints the kernel result as JSON; never prints credentials.

Exit codes:
    0  - success, nothing left to do
    1  - kernel reported success=False
    2  - activation gate blocked
    3  - success but reconciliation work remains (unresolved rows, deferred
         fill booking, a failed journal, truncated scan) or a dry run found
         pending work

Usage:
    uv run python -m scripts.kiwoom_mock_auto_reconcile
    uv run python -m scripts.kiwoom_mock_auto_reconcile --apply-terminal-only
    uv run python -m scripts.kiwoom_mock_auto_reconcile --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.services.kiwoom_mock_reconcile import (
    DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    FULL_GATE_ENVS,
    TERMINAL_GATE_ENV,
    US_TERMINAL_GATE_ENV,
    full_gate_enabled,
    kiwoom_mock_reconcile_orders_impl,
    terminal_gate_enabled,
)

_GATE_ENV_BY_MARKET = {"kr": TERMINAL_GATE_ENV, "us": US_TERMINAL_GATE_ENV}


def _work_remains(result: dict[str, Any]) -> bool:
    if result.get("candidate_scan", {}).get("truncated"):
        return True
    if result.get("unresolved_count"):
        return True
    actions = (row.get("action", "") for row in result.get("reconciled", []))
    return any(
        action.startswith("would_")
        or action in {"deferred_fill_booking", "noop_no_evidence"}
        for action in actions
    )


async def _run(
    *,
    market: str,
    dry_run: bool,
    terminal_only: bool,
    limit: int,
    min_request_interval_seconds: float,
) -> int:
    result = await kiwoom_mock_reconcile_orders_impl(
        market=market,
        dry_run=dry_run,
        terminal_only=terminal_only,
        limit=limit,
        min_request_interval_seconds=min_request_interval_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    if not result.get("success"):
        return 1
    return 3 if _work_remains(result) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kiwoom mock terminal reconcile (hk 4722)"
    )
    parser.add_argument(
        "--market",
        choices=("kr", "us"),
        default="kr",
        help="mock account to reconcile (default kr)",
    )
    parser.add_argument(
        "--apply-terminal-only",
        action="store_true",
        help=(
            "apply expired/cancelled/rejected and uncertain->accepted only; "
            "defer fill booking. Requires the market's terminal gate "
            f"({TERMINAL_GATE_ENV} for kr, {US_TERMINAL_GATE_ENV} for us) =true"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "full mode (kr only): also book execution_ledger fills and journals "
            "with account='kiwoom_mock'. Requires "
            + " and ".join(f"{name}=true" for name in FULL_GATE_ENVS)
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum ledger rows to scan (default 100)",
    )
    parser.add_argument(
        "--min-request-interval-seconds",
        type=float,
        default=DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        help=(
            "minimum spacing between broker reads "
            f"(default {DEFAULT_MIN_REQUEST_INTERVAL_SECONDS}; mock TRs are "
            "1 request/s)"
        ),
    )
    args = parser.parse_args(argv)

    if args.apply and args.apply_terminal_only:
        parser.error("--apply and --apply-terminal-only are mutually exclusive")
    if args.apply and args.market != "kr":
        parser.error(
            "--apply (full-mode fill booking) is KR only; "
            "use --apply-terminal-only for --market us"
        )

    gate_env = _GATE_ENV_BY_MARKET[args.market]
    if args.apply_terminal_only and not terminal_gate_enabled(market=args.market):
        # Refuse before any ledger or broker access (design §3.3).
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "paused",
                    "message": (f"--apply-terminal-only requires {gate_env}=true"),
                    "market": args.market,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    if args.apply and not full_gate_enabled():
        # Refuse before any ledger or broker access (design §3.3).
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "paused",
                    "required_env": list(FULL_GATE_ENVS),
                    "message": (
                        "--apply requires "
                        + " and ".join(f"{name}=true" for name in FULL_GATE_ENVS)
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    applying = args.apply or args.apply_terminal_only
    return asyncio.run(
        _run(
            market=args.market,
            dry_run=not applying,
            terminal_only=args.apply_terminal_only,
            limit=args.limit,
            min_request_interval_seconds=args.min_request_interval_seconds,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
