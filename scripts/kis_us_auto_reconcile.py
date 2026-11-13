"""Scheduleless KIS US reconciliation; preview unless explicitly applied."""

import argparse
import asyncio
import json

from app.core.config import settings
from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl


async def _run(*, apply: bool, terminal_only: bool) -> int:
    result = await live_reconcile_orders_impl(
        market="us", broker="kis", dry_run=not apply, terminal_only=terminal_only
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("success"):
        return 1
    actions = [row.get("action", "") for row in result.get("reconciled", [])]
    if (not apply and any(a.startswith("would_mark_") for a in actions)) or (
        apply and terminal_only and "deferred_fill_booking" in actions
    ):
        print("WARNING: reconciliation requires attention")
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS US live order reconciliation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--apply-terminal-only", action="store_true")
    args = parser.parse_args()
    if (
        args.apply_terminal_only
        and settings.KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED is not True
    ):
        print("--apply-terminal-only requires KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED")
        return 2
    if args.apply and not (
        settings.LIVE_AUTO_RECONCILE_ENABLED
        and not settings.LIVE_AUTO_RECONCILE_DRY_RUN
    ):
        print(
            "--apply requires LIVE_AUTO_RECONCILE_ENABLED and LIVE_AUTO_RECONCILE_DRY_RUN=false"
        )
        return 2
    return asyncio.run(
        _run(apply=args.apply or args.apply_terminal_only, terminal_only=not args.apply)
    )


if __name__ == "__main__":
    raise SystemExit(main())
