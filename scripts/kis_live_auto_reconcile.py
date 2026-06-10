#!/usr/bin/env python3
"""ROB-475 — operator CLI for KIS live auto-reconcile.

Runs the same kernel as the paused taskiq task (kis_live.reconcile_periodic).
Use on-demand or wire to a cron. dry_run defaults to True (preview verdicts);
pass --apply to book fills/journals. Reuses the ROB-395 evidence-gated kernel —
no new mutation path. Prints the counts/summary; never prints secrets.

Exit codes:
    0  - success
    1  - kernel reported success=False
    2  - --apply blocked by the ROB-487 activation gates

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


async def _run(*, dry_run: bool) -> int:
    result = await kis_live_reconcile_orders_impl(dry_run=dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if result.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS live auto-reconcile (ROB-475)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="book fills/journals (dry_run=False). Default is dry-run preview.",
    )
    args = parser.parse_args()
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
    return asyncio.run(_run(dry_run=not args.apply))


if __name__ == "__main__":
    sys.exit(main())
