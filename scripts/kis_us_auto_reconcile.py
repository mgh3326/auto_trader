"""Scheduleless KIS US reconciliation; preview unless explicitly applied."""

import argparse
import asyncio
import json

from app.core.config import settings
from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl


async def _run(*, apply: bool, terminal_only: bool) -> int:
    result = await live_reconcile_orders_impl(
        market="us",
        broker="kis",
        dry_run=not apply,
        scope="terminal_only" if terminal_only else "full",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    rows = result.get("reconciled", [])

    def clean(row: dict) -> bool:
        action = row.get("action", "")
        if (
            row.get("error") is not None
            or row.get("requires_manual_review")
            or row.get("reason_code") == "not_found"
        ):
            return False
        return action in {"noop_pending", "noop_terminal"} or (
            apply and action.startswith("marked_")
        )

    if (
        not result.get("success")
        or result.get("error") is not None
        or result.get("candidate_scan", {}).get("truncated")
        or result.get("proposal_rung_sweep", {}).get("error") is not None
        or result.get("proposal_projection_repair", {}).get("failed", 0) > 0
        or result.get("batch_build_error") is not None
        or not all(clean(row) for row in rows)
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
        _run(
            apply=args.apply or args.apply_terminal_only,
            terminal_only=args.apply_terminal_only,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
