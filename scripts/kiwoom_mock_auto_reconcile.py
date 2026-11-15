#!/usr/bin/env python3
"""hk 4722 (3797 T-P3) — operator CLI for Kiwoom mock terminal reconcile.

Previews verdicts by default and writes nothing. ``--apply-terminal-only``
closes only expired / cancelled / rejected rows and resolves ``uncertain`` to
``accepted``; fills are deferred to T-P4. There is no full-apply mode yet and no
scheduler — this lever is manual (hard rule 6).

Prints the kernel result as JSON; never prints credentials.

Exit codes:
    0  - success, nothing left to do
    1  - kernel reported success=False
    2  - activation gate blocked
    3  - success but reconciliation work remains (unresolved rows, deferred
         fill booking, truncated scan) or a dry run found pending work

Usage:
    uv run python -m scripts.kiwoom_mock_auto_reconcile
    uv run python -m scripts.kiwoom_mock_auto_reconcile --apply-terminal-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.services.kiwoom_mock_reconcile import (
    DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    TERMINAL_GATE_ENV,
    kiwoom_mock_reconcile_orders_impl,
    terminal_gate_enabled,
)


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
    dry_run: bool,
    terminal_only: bool,
    limit: int,
    min_request_interval_seconds: float,
) -> int:
    result = await kiwoom_mock_reconcile_orders_impl(
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
        "--apply-terminal-only",
        action="store_true",
        help=(
            "apply expired/cancelled/rejected and uncertain->accepted only; "
            "defer fill booking. Requires "
            f"{TERMINAL_GATE_ENV}=true"
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

    if args.apply_terminal_only and not terminal_gate_enabled():
        # Refuse before any ledger or broker access (design §3.3).
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "paused",
                    "message": (
                        f"--apply-terminal-only requires {TERMINAL_GATE_ENV}=true"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    return asyncio.run(
        _run(
            dry_run=not args.apply_terminal_only,
            terminal_only=args.apply_terminal_only,
            limit=args.limit,
            min_request_interval_seconds=args.min_request_interval_seconds,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
