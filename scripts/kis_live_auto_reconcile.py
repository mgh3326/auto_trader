#!/usr/bin/env python3
"""ROB-475 — operator CLI for KIS live auto-reconcile.

Runs the same kernel as the paused taskiq task (kis_live.reconcile_periodic).
Use on-demand or wire to a cron. dry_run defaults to True (preview verdicts);
pass --apply to book fills/journals. Reuses the ROB-395 evidence-gated kernel —
no new mutation path. Prints the counts/summary; never prints secrets.

Exit codes:
    0  - success
    1  - kernel reported success=False
    2  - activation gate blocked
    3  - success but reconciliation work remains

Usage:
    uv run python -m scripts.kis_live_auto_reconcile            # dry-run
    uv run python -m scripts.kis_live_auto_reconcile --apply    # book fills
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.config import settings
from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl


async def _run(*, dry_run: bool, terminal_only: bool = False) -> int:
    if terminal_only:
        result = await kis_live_reconcile_orders_impl(
            dry_run=False, scope="terminal_only"
        )
    else:
        result = await kis_live_reconcile_orders_impl(dry_run=dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    if not result.get("success"):
        return 1
    actions = (row.get("action", "") for row in result.get("reconciled", []))
    if terminal_only and any(action == "deferred_fill_booking" for action in actions):
        return 3
    if dry_run and any(action.startswith("would_") for action in actions):
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS live auto-reconcile (ROB-475)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--apply",
        action="store_true",
        help="book fills/journals (dry_run=False). Default is dry-run preview.",
    )
    group.add_argument(
        "--apply-terminal-only",
        action="store_true",
        help="apply expiry/cancellation only; defer fill booking",
    )
    args = parser.parse_args()
    if (
        args.apply_terminal_only
        and settings.KIS_LIVE_AUTO_RECONCILE_ENABLED is not True
    ):
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "paused",
                    "message": "--apply-terminal-only requires KIS_LIVE_AUTO_RECONCILE_ENABLED",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    if args.apply and not (
        settings.KIS_LIVE_AUTO_RECONCILE_ENABLED
        and settings.KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED
    ):
        # ROB-487: unattended (cron) application shares the taskiq activation
        # gates so the CLI cannot bypass the safety review. Manual one-off
        # booking stays available through the MCP tool (interactive, fail-closed
        # semantics).
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "paused",
                    "message": (
                        "--apply requires KIS_LIVE_AUTO_RECONCILE_ENABLED and "
                        "KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    return asyncio.run(
        _run(
            dry_run=not (args.apply or args.apply_terminal_only),
            terminal_only=args.apply_terminal_only,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
