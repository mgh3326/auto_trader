"""Scheduleless Toss reconciliation; preview unless explicitly applied."""

import argparse
import asyncio
import json

from app.core.config import settings
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl


async def _run(*, apply: bool, terminal_only: bool) -> int:
    result = await toss_reconcile_orders_impl(
        dry_run=not apply, terminal_only=terminal_only
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("success"):
        return 1
    actions = [row.get("action", "") for row in result.get("reconciled", [])]
    if (
        (terminal_only and "noop_contradictory_fill_evidence" in actions)
        or (not apply and any(a.startswith("would_mark_") for a in actions))
        or (
            apply
            and terminal_only
            and any(
                a in {"deferred_fill_booking", "projection_failed"} for a in actions
            )
        )
    ):
        print("WARNING: reconciliation requires attention")
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Toss live order reconciliation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--apply-terminal-only", action="store_true")
    args = parser.parse_args()
    if (
        args.apply_terminal_only
        and settings.TOSS_TERMINAL_AUTO_RECONCILE_ENABLED is not True
    ):
        print("--apply-terminal-only requires TOSS_TERMINAL_AUTO_RECONCILE_ENABLED")
        return 2
    if args.apply and not (
        settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED
        and settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED
    ):
        print(
            "--apply requires TOSS_LIVE_AUTO_RECONCILE_ENABLED and TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED"
        )
        return 2
    return asyncio.run(
        _run(apply=args.apply or args.apply_terminal_only, terminal_only=not args.apply)
    )


if __name__ == "__main__":
    raise SystemExit(main())
